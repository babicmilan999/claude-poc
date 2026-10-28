"""Triage: one short `triage`-alias call with structured output, grounded by the signal
snapshot (architecture section 3). Below the confidence threshold it returns `other`."""

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from core.enums import IssueType
from core.llm import LLMProvider, ModelAlias, StructuredOutputError, invoke_structured
from core.masking import mask_text
from core.prompts import triage as prompt
from core.signals import SignalSnapshot

log = logging.getLogger(__name__)


class TriageOutput(BaseModel):
    """Classify the customer's payment or card issue."""

    category: IssueType = Field(description="The issue category.")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 to 1.0.")
    secondary_category: IssueType | None = Field(default=None, description="Another plausible category, or null.")
    reasoning: str = Field(description="One short sentence naming the facts used.")


class TriageResult(BaseModel):
    category: IssueType
    confidence: float
    secondary_category: IssueType | None
    reasoning: str
    model_category: IssueType | None  # what the model said, before the threshold
    below_threshold: bool
    prompt_version: str = prompt.PROMPT_VERSION


def build_triage_messages(text: str, snapshot: SignalSnapshot | None) -> list[SystemMessage | HumanMessage]:
    snapshot_text = snapshot.to_prompt(audience="ops") if snapshot else "Signal snapshot: not available."
    return [
        SystemMessage(content=prompt.SYSTEM),
        HumanMessage(content=prompt.USER_TEMPLATE.format(complaint=mask_text(text.strip()), snapshot=snapshot_text)),
    ]


async def triage(
    llm: LLMProvider,
    text: str,
    snapshot: SignalSnapshot | None,
    *,
    threshold: float,
    config: RunnableConfig | None = None,
) -> TriageResult:
    """Classify an issue. Never raises on bad model output: it falls back to `other`."""
    messages = build_triage_messages(text, snapshot)
    try:
        out = await invoke_structured(llm.get(ModelAlias.TRIAGE), TriageOutput, messages, config=config)
    except StructuredOutputError as exc:
        log.warning("triage: unusable model output (%s)", exc)
        return TriageResult(
            category=IssueType.OTHER,
            confidence=0.0,
            secondary_category=None,
            reasoning="Triage output could not be parsed.",
            model_category=None,
            below_threshold=True,
        )
    below = out.confidence < threshold
    return TriageResult(
        category=IssueType.OTHER if below else out.category,
        confidence=out.confidence,
        secondary_category=out.secondary_category,
        reasoning=out.reasoning,
        model_category=out.category,
        below_threshold=below,
    )
