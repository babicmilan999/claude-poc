"""Postgres `InvestigationStore` (SQLAlchemy 2 async, the tables in `core.db.models`).

Writes by a worker go through `core.jobs.owned_by(job)` (and `JobQueue.finish`), so a
worker whose job was handed to another can no longer change it. The job queue itself is
`core.jobs.JobQueue`. Only explicit columns are selected for the graph's intake: the
answer-key columns are never read.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db import models as m
from core.enums import (
    ACTIVE_INVESTIGATION_STATUSES,
    CaseSource,
    CaseStatus,
    InvestigationStatus,
    InvestigationTrigger,
    IssueType,
    Verdict,
)
from core.jobs import ClaimedJob, JobQueue, owned_by
from investigation import schemas
from investigation.store import (
    CaseTransition,
    Conflict,
    EventSpec,
    IntakeData,
    JobLost,
    NewCase,
    NotFound,
    ReviewResult,
    RunOutcome,
    StartResult,
    resume_payload_from,
    status_event,
)

_ACTIVE = [s.value for s in ACTIVE_INVESTIGATION_STATUSES]

# Case columns returned by the API: never the answer keys.
_CASE_COLUMNS = (
    m.Case.id,
    m.Case.customer_id,
    m.Case.customer_name,
    m.Case.conversation_id,
    m.Case.source,
    m.Case.category,
    m.Case.complaint,
    m.Case.trigger_payload,
    m.Case.summary,
    m.Case.status,
    m.Case.simulated,
    m.Case.created_at,
    m.Case.updated_at,
)


def _active_id() -> Any:
    return (
        select(m.Investigation.id)
        .where(and_(m.Investigation.case_id == m.Case.id, m.Investigation.status.in_(_ACTIVE)))
        .limit(1)
        .correlate(m.Case)
        .scalar_subquery()
        .label("active_investigation_id")
    )


def _case_select() -> Select[Any]:
    return select(*_CASE_COLUMNS, _active_id())


def _case_view(row: Any) -> schemas.Case:
    return schemas.Case(
        id=row.id,
        customer_id=row.customer_id,
        customer_name=row.customer_name,
        conversation_id=row.conversation_id,
        source=CaseSource(row.source),
        category=IssueType(row.category) if row.category else None,
        complaint=row.complaint,
        trigger_payload=row.trigger_payload,
        summary=row.summary,
        status=CaseStatus(row.status),
        simulated=row.simulated,
        active_investigation_id=row.active_investigation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _review_view(review: m.Review | None) -> schemas.Review | None:
    if review is None:
        return None
    return schemas.Review(
        verdict=Verdict(review.verdict),
        corrected_issue_type=review.corrected_issue_type,  # type: ignore[arg-type]
        corrected_root_cause=review.corrected_root_cause,  # type: ignore[arg-type]
        hint=review.hint,
        notes=review.notes,
        created_at=review.created_at,
    )


def _diagnosis_view(diagnosis: m.Diagnosis | None) -> schemas.Diagnosis | None:
    if diagnosis is None:
        return None
    return schemas.Diagnosis(
        id=diagnosis.id,
        issue_type=diagnosis.issue_type,  # type: ignore[arg-type]
        root_cause=diagnosis.root_cause,  # type: ignore[arg-type]
        explanation=diagnosis.explanation,
        confidence=diagnosis.confidence,
        evidence_ids=list(diagnosis.evidence_ids),
        recommended_action=diagnosis.recommended_action,
    )


def _investigation_view(
    inv: m.Investigation, diagnosis: m.Diagnosis | None, review: m.Review | None
) -> schemas.Investigation:
    review_view = _review_view(review)
    return schemas.Investigation(
        id=inv.id,
        case_id=inv.case_id,
        trigger=InvestigationTrigger(inv.trigger),
        status=InvestigationStatus(inv.status),
        issue_type=IssueType(inv.issue_type) if inv.issue_type else None,
        verdict=review_view.verdict if review_view else None,
        queued_at=inv.queued_at,
        started_at=inv.started_at,
        finished_at=inv.finished_at,
        attempts=inv.attempts,
        steps_used=inv.steps_used,
        category_changed=bool(inv.category_changed),
        evidence=[schemas.Evidence.model_validate(e) for e in inv.evidence or []],
        diagnosis=_diagnosis_view(diagnosis),
        escalation=schemas.Escalation.model_validate(inv.escalation) if inv.escalation else None,
        review=review_view,
        error=inv.error,
    )


def _investigation_select() -> Any:
    return (
        select(m.Investigation, m.Diagnosis, m.Review)
        .outerjoin(m.Diagnosis, m.Diagnosis.investigation_id == m.Investigation.id)
        .outerjoin(m.Review, m.Review.investigation_id == m.Investigation.id)
    )


def _add_events(session: AsyncSession, investigation_id: uuid.UUID, events: list[EventSpec]) -> None:
    for type_, payload in events:
        session.add(m.InvestigationEvent(investigation_id=investigation_id, type=type_, payload=payload))


async def _set_case_status(session: AsyncSession, case_id: uuid.UUID, status: CaseStatus) -> list[CaseTransition]:
    row = (
        await session.execute(select(m.Case.status, m.Case.category).where(m.Case.id == case_id).with_for_update())
    ).first()
    if row is None or row.status == status.value:
        return []
    await session.execute(update(m.Case).where(m.Case.id == case_id).values(status=status.value, updated_at=func.now()))
    return [
        CaseTransition(
            case_id=case_id,
            previous_status=CaseStatus(row.status),
            status=status,
            category=IssueType(row.category) if row.category else None,
        )
    ]


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class SqlInvestigationStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], jobs: JobQueue) -> None:
        self._sessionmaker = sessionmaker
        self.jobs = jobs

    # --- cases -----------------------------------------------------------------------------

    async def create_case(self, new: NewCase) -> schemas.Case:
        case_id = uuid.uuid4()
        async with self._sessionmaker.begin() as session:
            session.add(
                m.Case(
                    id=case_id,
                    customer_id=new.customer_id,
                    customer_name=new.customer_name,
                    source=new.source.value,
                    complaint=new.complaint,
                    trigger_payload=new.trigger_payload,
                    status=CaseStatus.NEW.value,
                )
            )
            await session.flush()
            await self.jobs.enqueue(session, case_id, trigger=InvestigationTrigger.AUTO)
        case = await self.get_case(case_id)
        assert case is not None
        return case

    async def list_cases(
        self,
        *,
        statuses: list[CaseStatus],
        source: CaseSource | None,
        category: IssueType | None,
        limit: int,
        offset: int,
    ) -> tuple[list[schemas.Case], int]:
        conditions: list[Any] = [m.Case.status.in_([s.value for s in statuses])]
        if source is not None:
            conditions.append(m.Case.source == source.value)
        if category is not None:
            conditions.append(m.Case.category == category.value)
        async with self._sessionmaker() as session:
            total = (await session.execute(select(func.count()).select_from(m.Case).where(*conditions))).scalar_one()
            rows = (
                await session.execute(
                    _case_select().where(*conditions).order_by(m.Case.created_at, m.Case.id).limit(limit).offset(offset)
                )
            ).all()
        return [_case_view(r) for r in rows], int(total)

    async def get_case(self, case_id: uuid.UUID) -> schemas.Case | None:
        async with self._sessionmaker() as session:
            row = (await session.execute(_case_select().where(m.Case.id == case_id))).first()
        return _case_view(row) if row else None

    async def get_case_detail(self, case_id: uuid.UUID) -> schemas.CaseDetail | None:
        async with self._sessionmaker() as session:
            row = (await session.execute(_case_select().where(m.Case.id == case_id))).first()
            if row is None:
                return None
            evidence = (await session.execute(select(m.Case.handoff_evidence).where(m.Case.id == case_id))).scalar_one()
            messages: list[m.Message] = []
            if row.conversation_id is not None:
                messages = list(
                    (
                        await session.execute(
                            select(m.Message)
                            .where(m.Message.conversation_id == row.conversation_id)
                            .order_by(m.Message.id)
                        )
                    ).scalars()
                )
            investigations = (
                await session.execute(
                    _investigation_select()
                    .where(m.Investigation.case_id == case_id)
                    .order_by(m.Investigation.queued_at.desc(), m.Investigation.id)
                )
            ).all()
        return schemas.CaseDetail(
            **_case_view(row).model_dump(),
            transcript=[
                schemas.Message(
                    id=msg.id,
                    conversation_id=msg.conversation_id,
                    role=msg.role,  # type: ignore[arg-type]
                    content=msg.content,
                    sources=list(msg.sources or []),
                    rating=msg.rating,  # type: ignore[arg-type]
                    created_at=msg.created_at,
                )
                for msg in messages
            ],
            handoff_evidence=[schemas.Evidence.model_validate(e) for e in evidence or []],
            investigations=[_investigation_view(i, d, r).summary() for i, d, r in investigations],
        )

    async def start_investigation(self, case_id: uuid.UUID, *, reinvestigate: bool) -> StartResult:
        created_id: uuid.UUID | None = None
        superseded_id: uuid.UUID | None = None
        transitions: list[CaseTransition] = []
        try:
            async with self._sessionmaker.begin() as session:
                status = (
                    await session.execute(select(m.Case.status).where(m.Case.id == case_id).with_for_update())
                ).scalar_one_or_none()
                if status is None:
                    raise NotFound(str(case_id))
                if status == CaseStatus.RESOLVED.value:
                    raise Conflict("the case is resolved")
                active = (
                    await session.execute(
                        select(m.Investigation.id, m.Investigation.status)
                        .where(and_(m.Investigation.case_id == case_id, m.Investigation.status.in_(_ACTIVE)))
                        .with_for_update()
                    )
                ).first()
                if active is not None and not reinvestigate:
                    existing_id = active.id
                else:
                    if active is not None:
                        if active.status != InvestigationStatus.AWAITING_REVIEW.value:
                            raise Conflict(f"an investigation is {active.status}")
                        superseded_id = active.id
                        await session.execute(
                            update(m.Investigation)
                            .where(m.Investigation.id == active.id)
                            .values(status=InvestigationStatus.SUPERSEDED.value, finished_at=func.now())
                        )
                        _add_events(session, active.id, [status_event(InvestigationStatus.SUPERSEDED)])
                    investigation = await self.jobs.enqueue(session, case_id, trigger=InvestigationTrigger.MANUAL)
                    created_id = existing_id = investigation.id
                    transitions = await _set_case_status(session, case_id, CaseStatus.INVESTIGATING)
        except IntegrityError as exc:  # a concurrent click created one first
            raise Conflict("the case already has an active investigation") from exc
        view = await self.get_investigation(existing_id)
        assert view is not None
        return StartResult(
            investigation=view, created=created_id is not None, transitions=transitions, superseded_id=superseded_id
        )

    # --- investigations ---------------------------------------------------------------------

    async def get_investigation(self, investigation_id: uuid.UUID) -> schemas.Investigation | None:
        async with self._sessionmaker() as session:
            row = (await session.execute(_investigation_select().where(m.Investigation.id == investigation_id))).first()
        return _investigation_view(*row) if row else None

    async def investigation_status(self, investigation_id: uuid.UUID) -> InvestigationStatus | None:
        async with self._sessionmaker() as session:
            status = (
                await session.execute(select(m.Investigation.status).where(m.Investigation.id == investigation_id))
            ).scalar_one_or_none()
        return InvestigationStatus(status) if status else None

    async def record_review(self, investigation_id: uuid.UUID, feedback: schemas.FeedbackRequest) -> ReviewResult:
        async with self._sessionmaker.begin() as session:
            inv = (
                await session.execute(
                    select(m.Investigation.id, m.Investigation.status, m.Investigation.case_id)
                    .where(m.Investigation.id == investigation_id)
                    .with_for_update()
                )
            ).first()
            if inv is None:
                raise NotFound(str(investigation_id))
            if inv.status != InvestigationStatus.AWAITING_REVIEW.value:
                raise Conflict(f"the investigation is {inv.status}, not awaiting_review")
            diagnosis_id = (
                await session.execute(select(m.Diagnosis.id).where(m.Diagnosis.investigation_id == investigation_id))
            ).scalar_one_or_none()
            await session.execute(delete(m.Review).where(m.Review.investigation_id == investigation_id))
            review_id = uuid.uuid4()
            created_at = (
                await session.execute(
                    insert(m.Review)
                    .values(
                        id=review_id,
                        investigation_id=investigation_id,
                        diagnosis_id=diagnosis_id,
                        verdict=feedback.verdict.value,
                        corrected_issue_type=feedback.corrected_issue_type.value
                        if feedback.corrected_issue_type
                        else None,
                        corrected_root_cause=feedback.corrected_root_cause.value
                        if feedback.corrected_root_cause
                        else None,
                        hint=feedback.hint,
                        notes=feedback.notes,
                    )
                    .returning(m.Review.created_at)
                )
            ).scalar_one()
            review = schemas.Review(**feedback.model_dump(), created_at=created_at)
            payload = resume_payload_from(feedback, review_id)
            events: list[EventSpec] = [("review_recorded", {"review": review.model_dump(mode="json")})]
            rejected = feedback.verdict is Verdict.REJECTED
            if rejected:
                await self.jobs.requeue(session, investigation_id, payload)
                events.append(status_event(InvestigationStatus.QUEUED))
                transitions = await _set_case_status(session, inv.case_id, CaseStatus.INVESTIGATING)
            else:
                await session.execute(
                    update(m.Investigation)
                    .where(m.Investigation.id == investigation_id)
                    .values(status=InvestigationStatus.COMPLETED.value, finished_at=func.now(), worker_id=None)
                )
                events.append(status_event(InvestigationStatus.COMPLETED))
                if feedback.corrected_issue_type is not None:
                    await session.execute(
                        update(m.Case)
                        .where(m.Case.id == inv.case_id)
                        .values(category=feedback.corrected_issue_type.value, updated_at=func.now())
                    )
                transitions = await _set_case_status(session, inv.case_id, CaseStatus.RESOLVED)
            _add_events(session, investigation_id, events)
        view = await self.get_investigation(investigation_id)
        assert view is not None
        return ReviewResult(investigation=view, resume_payload=payload, requeued=rejected, transitions=transitions)

    # --- events -----------------------------------------------------------------------------

    async def append_event(
        self, investigation_id: uuid.UUID, type: str, payload: dict[str, Any]
    ) -> schemas.InvestigationEvent:
        async with self._sessionmaker.begin() as session:
            row = (
                await session.execute(
                    insert(m.InvestigationEvent)
                    .values(investigation_id=investigation_id, type=type, payload=payload)
                    .returning(m.InvestigationEvent.id, m.InvestigationEvent.created_at)
                )
            ).one()
        return schemas.InvestigationEvent(
            id=row.id,
            investigation_id=investigation_id,
            type=type,
            created_at=row.created_at,
            payload=payload,  # type: ignore[arg-type]
        )

    async def list_events(
        self, investigation_id: uuid.UUID, after_id: int | None = None
    ) -> list[schemas.InvestigationEvent]:
        query = select(m.InvestigationEvent).where(m.InvestigationEvent.investigation_id == investigation_id)
        if after_id is not None:
            query = query.where(m.InvestigationEvent.id > after_id)
        async with self._sessionmaker() as session:
            rows = (await session.execute(query.order_by(m.InvestigationEvent.id))).scalars().all()
        return [
            schemas.InvestigationEvent(
                id=e.id,
                investigation_id=e.investigation_id,
                type=e.type,  # type: ignore[arg-type]
                created_at=e.created_at,
                payload=e.payload,
            )
            for e in rows
        ]

    # --- queue (core.jobs) ---------------------------------------------------------------------

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        return await self.jobs.claim_next(worker_id)

    async def heartbeat(self, investigation_id: uuid.UUID, worker_id: str) -> bool:
        return await self.jobs.heartbeat(investigation_id, worker_id)

    async def release_for_retry(self, job: ClaimedJob, error: str, *, max_attempts: int) -> InvestigationStatus | None:
        return await self.jobs.release_for_retry(job, error, max_attempts=max_attempts)

    async def requeue_stale(
        self, *, stale_after: timedelta, max_attempts: int
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        return await self.jobs.requeue_stale(stale_after=stale_after, max_attempts=max_attempts)

    # --- the run ---------------------------------------------------------------------------------

    async def load_intake(self, case_id: uuid.UUID) -> IntakeData:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(
                        m.Case.id,
                        m.Case.customer_id,
                        m.Case.customer_name,
                        m.Case.source,
                        m.Case.category,
                        m.Case.complaint,
                        m.Case.trigger_payload,
                        m.Case.summary,
                        m.Case.handoff_evidence,
                        m.Case.conversation_id,
                    ).where(m.Case.id == case_id)
                )
            ).first()
            if row is None:
                raise NotFound(str(case_id))
            transcript: list[tuple[str, str]] = []
            if row.conversation_id is not None:
                transcript = [
                    (r.role, r.content)
                    for r in (
                        await session.execute(
                            select(m.Message.role, m.Message.content)
                            .where(m.Message.conversation_id == row.conversation_id)
                            .order_by(m.Message.id)
                        )
                    ).all()
                ]
        return IntakeData(
            case_id=row.id,
            customer_id=row.customer_id,
            customer_name=row.customer_name,
            source=CaseSource(row.source),
            category=IssueType(row.category) if row.category else None,
            complaint=row.complaint,
            trigger_payload=row.trigger_payload,
            summary=row.summary,
            handoff_evidence=list(row.handoff_evidence or []),
            transcript=transcript,
        )

    async def _owned_update(self, session: AsyncSession, job: ClaimedJob, **values: Any) -> None:
        result = await session.execute(update(m.Investigation).where(owned_by(job)).values(**values))
        if not _rowcount(result):
            raise JobLost(str(job.investigation_id))

    async def mark_started(self, job: ClaimedJob, events: list[EventSpec]) -> list[CaseTransition]:
        async with self._sessionmaker.begin() as session:
            await self._owned_update(session, job, error=None)
            _add_events(session, job.investigation_id, events)
            return await _set_case_status(session, job.case_id, CaseStatus.INVESTIGATING)

    async def record_classification(
        self, job: ClaimedJob, issue_type: IssueType, category_changed: bool | None = None
    ) -> list[CaseTransition]:
        values: dict[str, Any] = {"issue_type": issue_type.value}
        if category_changed is not None:
            values["category_changed"] = category_changed
        async with self._sessionmaker.begin() as session:
            await self._owned_update(session, job, **values)
            row = (
                await session.execute(
                    select(m.Case.status, m.Case.category).where(m.Case.id == job.case_id).with_for_update()
                )
            ).first()
            if row is None or row.category == issue_type.value:
                return []
            await session.execute(
                update(m.Case).where(m.Case.id == job.case_id).values(category=issue_type.value, updated_at=func.now())
            )
            return [CaseTransition(job.case_id, CaseStatus(row.status), CaseStatus(row.status), issue_type)]

    async def _store_outcome(self, session: AsyncSession, job: ClaimedJob, outcome: RunOutcome) -> None:
        values: dict[str, Any] = {
            "category_changed": outcome.category_changed,
            "steps_used": outcome.steps_used,
            "evidence": outcome.evidence,
            "escalation": outcome.escalation.model_dump(mode="json") if outcome.escalation else None,
        }
        if outcome.issue_type is not None:
            values["issue_type"] = outcome.issue_type.value
        await self._owned_update(session, job, **values)
        # One diagnosis and one review per investigation: earlier ones survive in the event log.
        await session.execute(delete(m.Review).where(m.Review.investigation_id == job.investigation_id))
        await session.execute(delete(m.Diagnosis).where(m.Diagnosis.investigation_id == job.investigation_id))
        if outcome.diagnosis is not None:
            d = outcome.diagnosis
            session.add(
                m.Diagnosis(
                    id=d.id,
                    investigation_id=job.investigation_id,
                    issue_type=IssueType(d.issue_type).value,
                    root_cause=str(d.root_cause),
                    explanation=d.explanation,
                    confidence=d.confidence,
                    evidence_ids=list(d.evidence_ids),
                    recommended_action=d.recommended_action,
                )
            )
            await session.flush()

    async def _finish(
        self,
        job: ClaimedJob,
        outcome: RunOutcome,
        events: list[EventSpec],
        status: InvestigationStatus,
        case_status: CaseStatus,
    ) -> list[CaseTransition]:
        async with self._sessionmaker.begin() as session:
            await self._store_outcome(session, job, outcome)
            if not await self.jobs.finish(job, status, session=session):
                raise JobLost(str(job.investigation_id))
            _add_events(session, job.investigation_id, events)
            return await _set_case_status(session, job.case_id, case_status)

    async def pause_for_review(
        self, job: ClaimedJob, outcome: RunOutcome, events: list[EventSpec]
    ) -> list[CaseTransition]:
        case_status = CaseStatus.AWAITING_REVIEW if outcome.diagnosis else CaseStatus.ESCALATED
        return await self._finish(job, outcome, events, InvestigationStatus.AWAITING_REVIEW, case_status)

    async def complete_parked(
        self, job: ClaimedJob, outcome: RunOutcome, events: list[EventSpec]
    ) -> list[CaseTransition]:
        return await self._finish(job, outcome, events, InvestigationStatus.COMPLETED, CaseStatus.NEEDS_MANUAL)

    async def fail_cases(self, investigation_ids: list[uuid.UUID], error: str) -> list[CaseTransition]:
        transitions: list[CaseTransition] = []
        async with self._sessionmaker.begin() as session:
            rows = (
                await session.execute(
                    select(m.Investigation.id, m.Investigation.case_id, m.Investigation.error).where(
                        and_(
                            m.Investigation.id.in_(investigation_ids),
                            m.Investigation.status == InvestigationStatus.FAILED.value,
                        )
                    )
                )
            ).all()
            for row in rows:
                _add_events(session, row.id, [status_event(InvestigationStatus.FAILED, row.error or error)])
                transitions += await _set_case_status(session, row.case_id, CaseStatus.NEEDS_MANUAL)
        return transitions
