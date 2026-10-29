"""What one eval item produced (the experiment task's output), as plain JSON-able models.

The output is built from what the agents did; the answer key is not part of it. Scorers
compare it with the scenario's `expected` block, which the task never sees.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from core.enums import ConversationOutcome, ExpectedOutcome, IssueType, RootCause


class Mode(StrEnum):
    JOURNEY = "journey"  # simulated customer → assistant → handoff → copilot
    ASSISTANT = "assistant"  # fixed conversation script, assistant only
    COPILOT = "copilot"  # fixed complaint as a simulated email case, copilot only


class Expected(BaseModel):
    """The scenario's answer key (`Expected` in `contracts/scenario.schema.json`)."""

    issue_type: IssueType
    root_cause: RootCause
    outcome: ExpectedOutcome


class AnswerOut(BaseModel):
    text: str
    sources: list[dict[str, Any]] = Field(default_factory=list)


class ConversationOut(BaseModel):
    conversation_id: str
    outcome: ConversationOutcome | None
    ended_by: str
    category: IssueType | None = None
    turns: int = 0
    case_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    transcript: list[dict[str, str]] = Field(default_factory=list)

    @property
    def handed_off(self) -> bool:
        return self.outcome is ConversationOutcome.HANDED_OFF


class CaseOut(BaseModel):
    case_id: str
    case_status: str
    investigation_id: str | None = None
    investigation_status: str | None = None
    provided_category: IssueType | None = None
    issue_type: IssueType | None = None  # the copilot's final classification
    diagnosis_issue_type: IssueType | None = None
    root_cause: RootCause | None = None
    confidence: float | None = None
    escalated: bool = False
    category_changed: bool = False
    steps_used: int = 0
    seconds_to_diagnosis: float | None = None


class ItemOutput(BaseModel):
    """The task's output (and the experiment trace's output): no scenario id, since ids
    like `card-decline-fraud-rule` name the answer."""

    mode: Mode
    conversation: ConversationOut | None = None
    answers: list[AnswerOut] = Field(default_factory=list)
    case: CaseOut | None = None
    error: str | None = None
    seconds: float = 0.0
