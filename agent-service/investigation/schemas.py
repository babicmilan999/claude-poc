"""Request and response bodies of the ops endpoints (`contracts/agent-api.yaml`, tags
`ops-cases` and `ops-investigations`), and the SSE event shapes.

None of these models has a field for the simulation answer keys (`expected_root_cause`,
`expected_outcome`): the stores never select them, so no response can carry them.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from core.enums import (
    CaseSource,
    CaseStatus,
    EvidenceOrigin,
    InvestigationEventType,
    InvestigationStatus,
    InvestigationTrigger,
    IssueType,
    MessageRole,
    Rating,
    RootCause,
    Verdict,
)


class _Out(BaseModel):
    model_config = ConfigDict(use_enum_values=True)


# --- evidence, diagnosis, review ---------------------------------------------------


class Evidence(_Out):
    id: str
    origin: EvidenceOrigin
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str
    data: Any = None
    created_at: datetime


class Diagnosis(_Out):
    id: uuid.UUID
    issue_type: IssueType
    root_cause: RootCause
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(min_length=1)
    recommended_action: str


class Escalation(_Out):
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class Review(_Out):
    verdict: Verdict
    corrected_issue_type: IssueType | None = None
    corrected_root_cause: RootCause | None = None
    hint: str | None = None
    notes: str | None = None
    created_at: datetime


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    corrected_issue_type: IssueType | None = None
    corrected_root_cause: RootCause | None = None
    hint: str | None = Field(default=None, min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _required_fields(self) -> "FeedbackRequest":
        if self.verdict is Verdict.CORRECTED and self.corrected_root_cause is None:
            raise ValueError("`corrected` requires corrected_root_cause")
        if self.verdict is Verdict.REJECTED and not (self.hint and self.hint.strip()):
            raise ValueError("`rejected` requires hint")
        return self


# --- investigations ----------------------------------------------------------------


class InvestigationSummary(_Out):
    id: uuid.UUID
    case_id: uuid.UUID
    trigger: InvestigationTrigger
    status: InvestigationStatus
    issue_type: IssueType | None = None
    verdict: Verdict | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Investigation(InvestigationSummary):
    attempts: int = 0
    steps_used: int = 0
    category_changed: bool = False
    evidence: list[Evidence] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    escalation: Escalation | None = None
    review: Review | None = None
    error: str | None = None

    def summary(self) -> InvestigationSummary:
        return InvestigationSummary.model_validate(self.model_dump(include=set(InvestigationSummary.model_fields)))


# --- cases ---------------------------------------------------------------------------


class Case(_Out):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None = None
    conversation_id: uuid.UUID | None = None
    source: CaseSource
    category: IssueType | None = None
    complaint: str | None = None
    trigger_payload: dict[str, Any] | None = None
    summary: str | None = None
    status: CaseStatus
    simulated: bool = False
    active_investigation_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    @model_serializer(mode="wrap")
    def _omit_unknown_name(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("customer_name") is None:  # a string in the contract, not nullable
            data.pop("customer_name", None)
        return data


class Message(_Out):
    id: int
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    rating: Rating | None = None
    created_at: datetime


class CaseDetail(Case):
    transcript: list[Message] = Field(default_factory=list)
    handoff_evidence: list[Evidence] = Field(default_factory=list)
    investigations: list[InvestigationSummary] = Field(default_factory=list)


class CasePage(_Out):
    items: list[Case]
    total: int


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    source: Literal["email", "phone", "ops_manual"]
    complaint: str | None = Field(default=None, min_length=1, max_length=8000)
    trigger_payload: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _complaint_or_payload(self) -> "CreateCaseRequest":
        if self.complaint is None and self.trigger_payload is None:
            raise ValueError("`complaint` or `trigger_payload` is required")
        return self


# --- events ----------------------------------------------------------------------------


class InvestigationEvent(_Out):
    id: int
    investigation_id: uuid.UUID
    type: InvestigationEventType
    created_at: datetime
    payload: dict[str, Any]


class CaseCreatedEvent(_Out):
    id: str
    type: Literal["case_created"] = "case_created"
    created_at: datetime
    case: Case


class CaseStatusChangedEvent(_Out):
    id: str
    type: Literal["status_changed"] = "status_changed"
    created_at: datetime
    case_id: uuid.UUID
    previous_status: CaseStatus | None = None
    status: CaseStatus
    category: IssueType | None = None


# Investigation statuses at which the SSE timeline closes.
STREAM_STOP_STATUSES = frozenset(
    {
        InvestigationStatus.AWAITING_REVIEW,
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.SUPERSEDED,
    }
)
