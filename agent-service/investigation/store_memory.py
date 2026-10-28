"""In-memory `InvestigationStore`: the same semantics as the Postgres store (one active
investigation per case, oldest job first, worker ownership), without a database.

Used by the unit tests and when the app runs without `DATABASE_URL` wiring. Every method
runs without awaiting in the middle, so each one is atomic on the event loop.
"""

import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.enums import (
    ACTIVE_INVESTIGATION_STATUSES,
    CaseSource,
    CaseStatus,
    InvestigationStatus,
    InvestigationTrigger,
    IssueType,
    Verdict,
)
from core.jobs import ClaimedJob
from investigation.schemas import (
    Case,
    CaseDetail,
    Diagnosis,
    Escalation,
    Evidence,
    FeedbackRequest,
    Investigation,
    InvestigationEvent,
    Message,
    Review,
)
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _CaseRow:
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None
    source: str
    status: str
    created_at: datetime
    updated_at: datetime
    conversation_id: uuid.UUID | None = None
    category: str | None = None
    complaint: str | None = None
    trigger_payload: dict[str, Any] | None = None
    summary: str | None = None
    handoff_evidence: list[dict[str, Any]] = field(default_factory=list)
    simulated: bool = False
    # Answer keys: stored for scoring like the real table, never read by the store.
    expected_root_cause: str | None = None
    expected_outcome: str | None = None


@dataclass
class _InvRow:
    id: uuid.UUID
    case_id: uuid.UUID
    trigger: str
    status: str
    queued_at: datetime
    seq: int
    issue_type: str | None = None
    category_changed: bool | None = None
    steps_used: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    escalation: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    resume_payload: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    diagnosis: dict[str, Any] | None = None
    review: dict[str, Any] | None = None


@dataclass
class _MessageRow:
    id: int
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    sources: list[dict[str, Any]] = field(default_factory=list)
    rating: str | None = None


class MemoryInvestigationStore:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self.cases: dict[uuid.UUID, _CaseRow] = {}
        self.investigations: dict[uuid.UUID, _InvRow] = {}
        self.events: list[InvestigationEvent] = []
        self.messages: list[_MessageRow] = []
        self._event_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._seq = itertools.count(1)

    # --- test helpers (what the assistant's handoff writes in production) ----------------

    def add_case(
        self,
        *,
        customer_id: str | uuid.UUID,
        source: CaseSource = CaseSource.EMAIL,
        complaint: str | None = None,
        category: IssueType | None = None,
        summary: str | None = None,
        handoff_evidence: list[dict[str, Any]] | None = None,
        transcript: list[tuple[str, str]] | None = None,
        customer_name: str | None = None,
        trigger_payload: dict[str, Any] | None = None,
        simulated: bool = False,
        expected_root_cause: str | None = None,
        expected_outcome: str | None = None,
        enqueue: bool = True,
        trigger: InvestigationTrigger = InvestigationTrigger.AUTO,
    ) -> uuid.UUID:
        now = self._clock()
        conversation_id = uuid.uuid4() if transcript else None
        case = _CaseRow(
            id=uuid.uuid4(),
            customer_id=uuid.UUID(str(customer_id)),
            customer_name=customer_name,
            source=CaseSource(source).value,
            status=CaseStatus.NEW.value,
            created_at=now,
            updated_at=now,
            conversation_id=conversation_id,
            category=IssueType(category).value if category else None,
            complaint=complaint,
            trigger_payload=trigger_payload,
            summary=summary,
            handoff_evidence=list(handoff_evidence or []),
            simulated=simulated,
            expected_root_cause=expected_root_cause,
            expected_outcome=expected_outcome,
        )
        self.cases[case.id] = case
        for role, content in transcript or []:
            assert conversation_id is not None
            self.messages.append(
                _MessageRow(
                    id=next(self._message_ids),
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    created_at=now,
                )
            )
        if enqueue:
            self._enqueue(case.id, trigger)
        return case.id

    # --- internals ---------------------------------------------------------------------------

    def _enqueue(self, case_id: uuid.UUID, trigger: InvestigationTrigger) -> _InvRow:
        if self._active(case_id) is not None:
            raise Conflict("the case already has an active investigation")
        row = _InvRow(
            id=uuid.uuid4(),
            case_id=case_id,
            trigger=trigger.value,
            status=InvestigationStatus.QUEUED.value,
            queued_at=self._clock(),
            seq=next(self._seq),
        )
        self.investigations[row.id] = row
        return row

    def _active(self, case_id: uuid.UUID) -> _InvRow | None:
        active = {s.value for s in ACTIVE_INVESTIGATION_STATUSES}
        return next((i for i in self.investigations.values() if i.case_id == case_id and i.status in active), None)

    def _owned(self, job: ClaimedJob) -> _InvRow:
        row = self.investigations.get(job.investigation_id)
        if row is None or row.status != InvestigationStatus.RUNNING.value or row.worker_id != job.worker_id:
            raise JobLost(str(job.investigation_id))
        return row

    def _append(self, investigation_id: uuid.UUID, events: list[EventSpec]) -> None:
        for type_, payload in events:
            self.events.append(
                InvestigationEvent(
                    id=next(self._event_ids),
                    investigation_id=investigation_id,
                    type=type_,  # type: ignore[arg-type]
                    created_at=self._clock(),
                    payload=payload,
                )
            )

    def _set_case_status(self, case_id: uuid.UUID, status: CaseStatus) -> list[CaseTransition]:
        case = self.cases[case_id]
        previous = CaseStatus(case.status)
        if previous is status:
            return []
        case.status = status.value
        case.updated_at = self._clock()
        return [
            CaseTransition(
                case_id=case.id,
                previous_status=previous,
                status=status,
                category=IssueType(case.category) if case.category else None,
            )
        ]

    def _case_view(self, case: _CaseRow) -> Case:
        active = self._active(case.id)
        return Case(
            id=case.id,
            customer_id=case.customer_id,
            customer_name=case.customer_name,
            conversation_id=case.conversation_id,
            source=CaseSource(case.source),
            category=IssueType(case.category) if case.category else None,
            complaint=case.complaint,
            trigger_payload=case.trigger_payload,
            summary=case.summary,
            status=CaseStatus(case.status),
            simulated=case.simulated,
            active_investigation_id=active.id if active else None,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )

    def _inv_view(self, row: _InvRow) -> Investigation:
        review = Review.model_validate(row.review) if row.review else None
        return Investigation(
            id=row.id,
            case_id=row.case_id,
            trigger=InvestigationTrigger(row.trigger),
            status=InvestigationStatus(row.status),
            issue_type=IssueType(row.issue_type) if row.issue_type else None,
            verdict=review.verdict if review else None,
            queued_at=row.queued_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            attempts=row.attempts,
            steps_used=row.steps_used,
            category_changed=bool(row.category_changed),
            evidence=[Evidence.model_validate(e) for e in row.evidence],
            diagnosis=Diagnosis.model_validate(row.diagnosis) if row.diagnosis else None,
            escalation=Escalation.model_validate(row.escalation) if row.escalation else None,
            review=review,
            error=row.error,
        )

    # --- cases ----------------------------------------------------------------------------------

    async def create_case(self, new: NewCase) -> Case:
        case_id = self.add_case(
            customer_id=new.customer_id,
            source=new.source,
            complaint=new.complaint,
            trigger_payload=new.trigger_payload,
            customer_name=new.customer_name,
        )
        return self._case_view(self.cases[case_id])

    async def list_cases(
        self,
        *,
        statuses: list[CaseStatus],
        source: CaseSource | None,
        category: IssueType | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Case], int]:
        wanted = {s.value for s in statuses}
        rows = [
            c
            for c in self.cases.values()
            if c.status in wanted
            and (source is None or c.source == source.value)
            and (category is None or c.category == category.value)
        ]
        rows.sort(key=lambda c: c.created_at)
        return [self._case_view(c) for c in rows[offset : offset + limit]], len(rows)

    async def get_case(self, case_id: uuid.UUID) -> Case | None:
        case = self.cases.get(case_id)
        return self._case_view(case) if case else None

    async def get_case_detail(self, case_id: uuid.UUID) -> CaseDetail | None:
        case = self.cases.get(case_id)
        if case is None:
            return None
        transcript = [
            Message(
                id=m.id,
                conversation_id=m.conversation_id,
                role=m.role,  # type: ignore[arg-type]
                content=m.content,
                sources=m.sources,
                rating=m.rating,  # type: ignore[arg-type]
                created_at=m.created_at,
            )
            for m in self.messages
            if case.conversation_id is not None and m.conversation_id == case.conversation_id
        ]
        investigations = sorted(
            (i for i in self.investigations.values() if i.case_id == case_id), key=lambda i: i.seq, reverse=True
        )
        return CaseDetail(
            **self._case_view(case).model_dump(),
            transcript=transcript,
            handoff_evidence=[Evidence.model_validate(e) for e in case.handoff_evidence],
            investigations=[self._inv_view(i).summary() for i in investigations],
        )

    async def start_investigation(self, case_id: uuid.UUID, *, reinvestigate: bool) -> StartResult:
        case = self.cases.get(case_id)
        if case is None:
            raise NotFound(str(case_id))
        if case.status == CaseStatus.RESOLVED.value:
            raise Conflict("the case is resolved")
        active = self._active(case_id)
        if active is not None and not reinvestigate:
            return StartResult(investigation=self._inv_view(active), created=False)
        superseded_id = None
        if active is not None:
            if active.status != InvestigationStatus.AWAITING_REVIEW.value:
                raise Conflict(f"an investigation is {active.status}")
            superseded_id = active.id
            active.status = InvestigationStatus.SUPERSEDED.value
            active.finished_at = self._clock()
            self._append(active.id, [status_event(InvestigationStatus.SUPERSEDED)])
        row = self._enqueue(case_id, InvestigationTrigger.MANUAL)
        transitions = self._set_case_status(case_id, CaseStatus.INVESTIGATING)
        return StartResult(
            investigation=self._inv_view(row), created=True, transitions=transitions, superseded_id=superseded_id
        )

    # --- investigations --------------------------------------------------------------------

    async def get_investigation(self, investigation_id: uuid.UUID) -> Investigation | None:
        row = self.investigations.get(investigation_id)
        return self._inv_view(row) if row else None

    async def investigation_status(self, investigation_id: uuid.UUID) -> InvestigationStatus | None:
        row = self.investigations.get(investigation_id)
        return InvestigationStatus(row.status) if row else None

    async def record_review(self, investigation_id: uuid.UUID, feedback: FeedbackRequest) -> ReviewResult:
        row = self.investigations.get(investigation_id)
        if row is None:
            raise NotFound(str(investigation_id))
        if row.status != InvestigationStatus.AWAITING_REVIEW.value:
            raise Conflict(f"the investigation is {row.status}, not awaiting_review")
        review = Review(**feedback.model_dump(), created_at=self._clock())
        row.review = review.model_dump(mode="json")
        payload = resume_payload_from(feedback, uuid.uuid4())
        events: list[EventSpec] = [("review_recorded", {"review": row.review})]
        if feedback.verdict is Verdict.REJECTED:
            row.status = InvestigationStatus.QUEUED.value
            row.resume_payload = payload
            row.worker_id = None
            events.append(status_event(InvestigationStatus.QUEUED))
            transitions = self._set_case_status(row.case_id, CaseStatus.INVESTIGATING)
        else:
            row.status = InvestigationStatus.COMPLETED.value
            row.finished_at = self._clock()
            events.append(status_event(InvestigationStatus.COMPLETED))
            if feedback.corrected_issue_type is not None:
                self.cases[row.case_id].category = feedback.corrected_issue_type.value
            transitions = self._set_case_status(row.case_id, CaseStatus.RESOLVED)
        self._append(row.id, events)
        return ReviewResult(
            investigation=self._inv_view(row),
            resume_payload=payload,
            requeued=feedback.verdict is Verdict.REJECTED,
            transitions=transitions,
        )

    # --- events -----------------------------------------------------------------------------

    async def append_event(self, investigation_id: uuid.UUID, type: str, payload: dict[str, Any]) -> InvestigationEvent:
        self._append(investigation_id, [(type, payload)])
        return self.events[-1]

    async def list_events(self, investigation_id: uuid.UUID, after_id: int | None = None) -> list[InvestigationEvent]:
        return [
            e for e in self.events if e.investigation_id == investigation_id and (after_id is None or e.id > after_id)
        ]

    # --- queue -------------------------------------------------------------------------------

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        queued = [i for i in self.investigations.values() if i.status == InvestigationStatus.QUEUED.value]
        if not queued:
            return None
        row = min(queued, key=lambda i: (i.queued_at, i.seq))
        row.status = InvestigationStatus.RUNNING.value
        row.attempts += 1
        row.worker_id = worker_id
        row.started_at = row.started_at or self._clock()
        row.heartbeat_at = self._clock()
        return ClaimedJob(
            investigation_id=row.id,
            case_id=row.case_id,
            trigger=row.trigger,
            attempts=row.attempts,
            resume_payload=row.resume_payload,
            worker_id=worker_id,
        )

    async def heartbeat(self, investigation_id: uuid.UUID, worker_id: str) -> bool:
        row = self.investigations.get(investigation_id)
        if row is None or row.status != InvestigationStatus.RUNNING.value or row.worker_id != worker_id:
            return False
        row.heartbeat_at = self._clock()
        return True

    async def release_for_retry(self, job: ClaimedJob, error: str, *, max_attempts: int) -> InvestigationStatus | None:
        try:
            row = self._owned(job)
        except JobLost:
            return None
        retry = job.attempts < max_attempts
        row.status = (InvestigationStatus.QUEUED if retry else InvestigationStatus.FAILED).value
        row.worker_id = None
        row.error = error[:2000]
        row.finished_at = None if retry else self._clock()
        return InvestigationStatus(row.status)

    async def requeue_stale(
        self, *, stale_after: timedelta, max_attempts: int
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        cutoff = self._clock() - stale_after
        requeued, failed = [], []
        for row in self.investigations.values():
            if row.status != InvestigationStatus.RUNNING.value or not row.heartbeat_at or row.heartbeat_at >= cutoff:
                continue
            row.worker_id = None
            if row.attempts < max_attempts:
                row.status = InvestigationStatus.QUEUED.value
                requeued.append(row.id)
            else:
                row.status = InvestigationStatus.FAILED.value
                row.error = "worker stopped responding; retries used up"
                row.finished_at = self._clock()
                failed.append(row.id)
        return requeued, failed

    # --- the run -------------------------------------------------------------------------------

    async def load_intake(self, case_id: uuid.UUID) -> IntakeData:
        case = self.cases.get(case_id)
        if case is None:
            raise NotFound(str(case_id))
        transcript = [
            (m.role, m.content)
            for m in self.messages
            if case.conversation_id is not None and m.conversation_id == case.conversation_id
        ]
        return IntakeData(
            case_id=case.id,
            customer_id=case.customer_id,
            customer_name=case.customer_name,
            source=CaseSource(case.source),
            category=IssueType(case.category) if case.category else None,
            complaint=case.complaint,
            trigger_payload=case.trigger_payload,
            summary=case.summary,
            handoff_evidence=list(case.handoff_evidence),
            transcript=transcript,
        )

    async def mark_started(self, job: ClaimedJob, events: list[EventSpec]) -> list[CaseTransition]:
        row = self._owned(job)
        row.error = None
        self._append(row.id, events)
        return self._set_case_status(row.case_id, CaseStatus.INVESTIGATING)

    async def record_classification(
        self, job: ClaimedJob, issue_type: IssueType, category_changed: bool | None = None
    ) -> list[CaseTransition]:
        row = self._owned(job)
        row.issue_type = issue_type.value
        if category_changed is not None:
            row.category_changed = category_changed
        case = self.cases[row.case_id]
        if case.category == issue_type.value:
            return []
        case.category = issue_type.value
        case.updated_at = self._clock()
        return [CaseTransition(case.id, CaseStatus(case.status), CaseStatus(case.status), issue_type)]

    def _store_outcome(self, row: _InvRow, outcome: RunOutcome) -> None:
        row.issue_type = outcome.issue_type.value if outcome.issue_type else row.issue_type
        row.category_changed = outcome.category_changed
        row.steps_used = outcome.steps_used
        row.evidence = list(outcome.evidence)
        row.diagnosis = outcome.diagnosis.model_dump(mode="json") if outcome.diagnosis else None
        row.escalation = outcome.escalation.model_dump(mode="json") if outcome.escalation else None
        row.review = None  # an earlier (rejected) review survives in the event log only
        row.worker_id = None

    async def pause_for_review(
        self, job: ClaimedJob, outcome: RunOutcome, events: list[EventSpec]
    ) -> list[CaseTransition]:
        row = self._owned(job)
        self._store_outcome(row, outcome)
        row.status = InvestigationStatus.AWAITING_REVIEW.value
        row.error = None
        self._append(row.id, events)
        status = CaseStatus.AWAITING_REVIEW if outcome.diagnosis else CaseStatus.ESCALATED
        return self._set_case_status(row.case_id, status)

    async def complete_parked(
        self, job: ClaimedJob, outcome: RunOutcome, events: list[EventSpec]
    ) -> list[CaseTransition]:
        row = self._owned(job)
        self._store_outcome(row, outcome)
        row.status = InvestigationStatus.COMPLETED.value
        row.finished_at = self._clock()
        row.error = None
        self._append(row.id, events)
        return self._set_case_status(row.case_id, CaseStatus.NEEDS_MANUAL)

    async def fail_cases(self, investigation_ids: list[uuid.UUID], error: str) -> list[CaseTransition]:
        transitions: list[CaseTransition] = []
        for investigation_id in investigation_ids:
            row = self.investigations.get(investigation_id)
            if row is None or row.status != InvestigationStatus.FAILED.value:
                continue
            self._append(row.id, [status_event(InvestigationStatus.FAILED, row.error or error)])
            transitions += self._set_case_status(row.case_id, CaseStatus.NEEDS_MANUAL)
        return transitions
