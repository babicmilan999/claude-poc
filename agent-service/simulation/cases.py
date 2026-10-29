"""Simulated cases with their answer key.

`InvestigationService.create_case` (`NewCase`) has no fields for `simulated` or the answer
key, so the Simulate email case is written here, the same way the assistant writes its
handoff case: in Postgres, the case row and its queued investigation in one transaction,
then `NOTIFY case_events` (the investigation package's listener announces it on the case
queue); without a database, through the in-memory store.

`investigation_handoff_sink` is the assistant store's `handoff_sink` for in-process eval
runs on in-memory stores (in Postgres both packages share the `cases` table): at handoff
the assistant's in-memory store hands the case, with its transcript and hidden answer
key, to the investigation's in-memory store, like `assistant.api` does in the app
without a database (that one finds the investigation service through the `AppContext`).
"""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.store import HandoffCaseCopy, HandoffCaseRef, HandoffSink
from core.db.models import Case as CaseRow
from core.enums import CaseSource, CaseStatus
from core.jobs import CASE_EVENTS_CHANNEL, JobQueue, notify
from investigation.schemas import Case
from investigation.service import InvestigationService
from investigation.store_memory import MemoryInvestigationStore
from simulation.banking import AnswerKey
from simulation.persona import CustomerView


class CaseStoreUnavailable(Exception):
    """Neither an in-memory investigation store nor a database to write the case to."""


async def create_simulated_email_case(
    investigation: InvestigationService,
    view: CustomerView,
    key: AnswerKey,
    complaint: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    jobs: JobQueue | None,
) -> Case:
    """A `simulated_email` case with no category, queued for an automatic investigation."""
    store = investigation.store
    if isinstance(store, MemoryInvestigationStore):
        case_id = store.add_case(
            customer_id=view.customer_id,
            source=CaseSource.SIMULATED_EMAIL,
            complaint=complaint,
            customer_name=view.customer_name,
            simulated=True,
            expected_root_cause=key.root_cause.value,
            expected_outcome=key.outcome.value,
        )
        case = await store.get_case(case_id)
        assert case is not None
        investigation.publish_case_created(case)
        return case

    if sessionmaker is None or jobs is None:
        raise CaseStoreUnavailable("no database for simulated cases")
    case_id = uuid.uuid4()
    async with sessionmaker.begin() as session:
        session.add(
            CaseRow(
                id=case_id,
                customer_id=view.customer_id,
                customer_name=view.customer_name,
                source=CaseSource.SIMULATED_EMAIL.value,
                complaint=complaint,
                status=CaseStatus.NEW.value,
                simulated=True,
                # Answer key: stored for scoring only, never read by the investigation.
                expected_root_cause=key.root_cause.value,
                expected_outcome=key.outcome.value,
            )
        )
        await session.flush()
        await jobs.enqueue(session, case_id)
        # The same payload as the assistant's handoff: the investigation listener loads
        # the case and announces it on `GET /cases/events`.
        await notify(session, CASE_EVENTS_CHANNEL, json.dumps({"type": "case_created", "case_id": str(case_id)}))
    case = await store.get_case(case_id)
    if case is None:  # pragma: no cover - written above
        raise CaseStoreUnavailable(f"case {case_id} not found after writing it")
    return case


def investigation_handoff_sink(investigation: InvestigationService) -> HandoffSink:
    """Takes each handoff case into the investigation's in-memory store and announces it."""
    store = investigation.store
    if not isinstance(store, MemoryInvestigationStore):
        raise CaseStoreUnavailable("handoff sinks are for the in-memory investigation store")

    async def sink(case: HandoffCaseCopy) -> HandoffCaseRef | None:
        case_id = store.add_case(
            customer_id=case.customer_id,
            source=CaseSource.ASSISTANT_HANDOFF,
            conversation_id=case.conversation_id,
            complaint=case.complaint,
            category=case.category,
            summary=case.summary,
            handoff_evidence=list(case.evidence),
            transcript=list(case.transcript),
            customer_name=case.customer_name,
            simulated=case.simulated,
            # Answer key: stored on the case for scoring only, never read by the investigation.
            expected_root_cause=case.expected_root_cause.value if case.expected_root_cause else None,
            expected_outcome=case.expected_outcome.value if case.expected_outcome else None,
        )
        created = await store.get_case(case_id)
        if created is not None:
            investigation.publish_case_created(created)
        return HandoffCaseRef(case_id, getattr(created, "active_investigation_id", None))

    return sink
