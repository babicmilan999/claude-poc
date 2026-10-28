"""Graph state and the structured outputs of the investigation graph (architecture
section 6).

State values are plain JSON (dicts, lists, strings) so every checkpointer can store them.
There is no field for the simulation answer keys.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from core.enums import IssueType, RootCause, Verdict


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    case_id: str
    customer_id: str
    trigger: Literal["auto", "manual"]
    # intake
    customer_profile: str | None
    complaint: str | None
    transcript_summary: str | None  # from an assistant handoff
    trigger_payload: dict[str, Any] | None
    provided_category: str | None  # from an assistant handoff
    snapshot: dict[str, Any] | None  # SignalSnapshot, JSON
    # classification
    issue_type: str | None
    classification_method: str | None  # verified | triaged
    category_changed: bool
    # investigation
    evidence: Annotated[list[dict[str, Any]], operator.add]  # Evidence, JSON; incl. the assistant's
    steps_used: int
    budget_base: int  # steps_used when this run (or the current reject round) started
    passes: int  # investigator passes in the current round
    findings: str | None  # the last investigator's summary
    assessment: dict[str, Any] | None  # EvidenceAssessment, JSON
    # outcome
    diagnosis: dict[str, Any] | None  # Diagnosis (contract shape), JSON
    escalation: dict[str, Any] | None
    reviewer_feedback: str | None  # the hint of the last rejection
    review: dict[str, Any] | None
    last_review_id: str | None  # the last resume payload applied (guards double resumes)
    outcome: str | None  # parked | finalized
    final: dict[str, Any] | None


class EvidenceAssessment(BaseModel):
    """Decide whether the evidence collected so far is enough to name a root cause."""

    enough: bool = Field(description="True only if the cited tool evidence clearly shows the root cause.")
    evidence_ids: list[str] = Field(
        default_factory=list, description="Ids of the evidence items (for example ev-3) that show it."
    )
    missing: str = Field(default="", description="If not enough: what is still unknown, in one sentence.")


class DiagnosisOutput(BaseModel):
    """The diagnosis of the case, grounded in the cited tool evidence."""

    issue_type: IssueType = Field(description="The issue category.")
    root_cause: RootCause = Field(description="The root cause code; unknown if the evidence does not show one.")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 to 1.0.")
    evidence_ids: list[str] = Field(min_length=1, description="Ids of the evidence items that prove it (ev-N).")
    recommended_action: str = Field(description="The next action for the ops agent, one or two sentences.")


class ReviewDecision(BaseModel):
    """The value `interrupt()` returns: the ops agent's review (FeedbackRequest + id)."""

    verdict: Verdict
    corrected_issue_type: IssueType | None = None
    corrected_root_cause: RootCause | None = None
    hint: str | None = None
    notes: str | None = None
    review_id: str | None = None


INVESTIGATOR_NODES: dict[IssueType, str] = {
    IssueType.CARD_DECLINE: "card_decline_investigator",
    IssueType.MISSED_PAYMENT: "missed_payment_investigator",
    IssueType.FAILED_DIRECT_DEBIT: "failed_direct_debit_investigator",
    IssueType.OTHER: "generic_investigator",
}
