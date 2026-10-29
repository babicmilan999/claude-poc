"""Reading where a case's investigation ended, through the investigation store's public
read methods (the same views the ops console gets: no answer keys)."""

import uuid

from core.enums import InvestigationStatus, IssueType, RootCause
from evals.results import CaseOut
from investigation.store import InvestigationStore

# Investigation statuses after which nothing changes without a human.
SETTLED_STATUSES = frozenset(
    {
        InvestigationStatus.AWAITING_REVIEW.value,
        InvestigationStatus.COMPLETED.value,
        InvestigationStatus.FAILED.value,
        InvestigationStatus.SUPERSEDED.value,
    }
)


async def read_case_outcome(
    store: InvestigationStore,
    case_id: uuid.UUID,
    *,
    provided_category: IssueType | None = None,
) -> CaseOut | None:
    """The case and its latest investigation, or None for an unknown case."""
    detail = await store.get_case_detail(case_id)
    if detail is None:
        return None
    out = CaseOut(case_id=str(case_id), case_status=str(detail.status), provided_category=provided_category)
    if not detail.investigations:
        return out
    latest = detail.investigations[0]  # newest first
    investigation = await store.get_investigation(latest.id)
    if investigation is None:
        return out
    out.investigation_id = str(investigation.id)
    out.investigation_status = str(investigation.status)
    out.issue_type = IssueType(investigation.issue_type) if investigation.issue_type else None
    out.category_changed = bool(investigation.category_changed)
    out.steps_used = investigation.steps_used
    out.escalated = investigation.escalation is not None
    if investigation.diagnosis is not None:
        out.diagnosis_issue_type = IssueType(investigation.diagnosis.issue_type)
        out.root_cause = RootCause(investigation.diagnosis.root_cause)
        out.confidence = investigation.diagnosis.confidence
    return out


def is_settled(outcome: CaseOut | None) -> bool:
    return outcome is not None and outcome.investigation_status in SETTLED_STATUSES
