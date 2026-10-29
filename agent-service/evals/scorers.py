"""Scorers (architecture section 10). Plain functions of an item's output and the
scenario's expected block; `None` means "does not apply to this item".

Deterministic: `handoff_correct`, `containment`, `assistant_category_correct`,
`copilot_category_correct`, `diagnosis_correct`, plus tracked numbers (`steps_used`,
`category_changed`, `seconds`). LLM-as-judge: `groundedness` (`judge` alias).
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from core.enums import ConversationOutcome, ExpectedOutcome, IssueType, RootCause
from core.llm import LLMProvider, ModelAlias, StructuredOutputError, invoke_structured
from core.masking import mask_data, mask_text
from evals import prompts
from evals.results import AnswerOut, Expected, ItemOutput, Mode


@dataclass(frozen=True)
class Score:
    name: str
    value: float
    comment: str | None = None


# --- building blocks (also used by the Simulate buttons) ---------------------------------


def handoff_value(handed_off: bool, expected: ExpectedOutcome) -> float:
    """1 when the assistant handed off a `needs_human` incident or kept a `self_serve` one."""
    return 1.0 if handed_off == (expected is ExpectedOutcome.NEEDS_HUMAN) else 0.0


def diagnosis_value(
    issue_type: IssueType | None, root_cause: RootCause | None, expected_issue: IssueType, expected_root: RootCause
) -> float:
    """1 when the diagnosis names the expected (issue type, root cause)."""
    return 1.0 if issue_type is expected_issue and root_cause is expected_root else 0.0


# --- deterministic scorers ---------------------------------------------------------------


def handoff_correct(output: ItemOutput, expected: Expected) -> Score | None:
    conv = output.conversation
    if conv is None or output.mode is Mode.COPILOT:
        return None
    value = handoff_value(conv.handed_off, expected.outcome)
    return Score("handoff_correct", value, f"{conv.outcome or 'open'}; expected {expected.outcome.value}")


def containment(output: ItemOutput, expected: Expected) -> Score | None:
    """Simulated journeys of `self_serve` incidents: did the customer leave resolved?"""
    conv = output.conversation
    if conv is None or output.mode is not Mode.JOURNEY or expected.outcome is not ExpectedOutcome.SELF_SERVE:
        return None
    value = 1.0 if conv.outcome is ConversationOutcome.RESOLVED else 0.0
    return Score("containment", value, f"{conv.outcome or 'open'} ({conv.ended_by})")


def assistant_category_correct(output: ItemOutput, expected: Expected) -> Score | None:
    conv = output.conversation
    if conv is None or output.mode is Mode.COPILOT:
        return None
    value = 1.0 if conv.category is expected.issue_type else 0.0
    return Score("assistant_category_correct", value, f"{conv.category or 'none'}; expected {expected.issue_type}")


def copilot_category_correct(output: ItemOutput, expected: Expected) -> Score | None:
    case = output.case
    if case is None:
        return None
    value = 1.0 if case.issue_type is expected.issue_type else 0.0
    return Score("copilot_category_correct", value, f"{case.issue_type or 'none'}; expected {expected.issue_type}")


def diagnosis_correct(output: ItemOutput, expected: Expected) -> Score | None:
    case = output.case
    if case is None:
        return None
    if case.root_cause is None:
        what = "escalated" if case.escalated else f"no diagnosis ({case.investigation_status or case.case_status})"
        return Score("diagnosis_correct", 0.0, what)
    value = diagnosis_value(case.diagnosis_issue_type, case.root_cause, expected.issue_type, expected.root_cause)
    return Score(
        "diagnosis_correct",
        value,
        f"{case.diagnosis_issue_type}/{case.root_cause}; expected {expected.issue_type}/{expected.root_cause}",
    )


def category_changed(output: ItemOutput, expected: Expected) -> Score | None:
    """Tracked: verification changed the assistant's category (handed-off cases)."""
    case = output.case
    if case is None or case.provided_category is None:
        return None
    return Score("category_changed", 1.0 if case.category_changed else 0.0)


def steps_used(output: ItemOutput, expected: Expected) -> Score | None:
    case = output.case
    if case is None:
        return None
    return Score("steps_used", float(case.steps_used))


def item_ok(output: ItemOutput, expected: Expected) -> Score | None:
    """1 when the item ran to the end without an error."""
    return Score("item_ok", 0.0 if output.error else 1.0, output.error)


def seconds(output: ItemOutput, expected: Expected) -> Score | None:
    return Score("seconds", round(output.seconds, 3))


Scorer = Callable[[ItemOutput, Expected], Score | None]

DETERMINISTIC_SCORERS: tuple[Scorer, ...] = (
    item_ok,
    handoff_correct,
    containment,
    assistant_category_correct,
    copilot_category_correct,
    diagnosis_correct,
    category_changed,
    steps_used,
    seconds,
)

# Reported but never gated: they are measurements, not pass/fail scores.
TRACKED_ONLY = frozenset({"steps_used", "category_changed", "seconds"})


# --- groundedness (LLM-as-judge) -----------------------------------------------------------


class GroundednessVerdict(BaseModel):
    """How many of the answer's factual claims the sources back."""

    claims: int = Field(ge=0, description="Number of factual claims in the answer.")
    supported: int = Field(ge=0, description="Number of those claims a source backs.")
    unsupported_claims: list[str] = Field(default_factory=list, description="Claims no source backs.")
    reason: str = Field(default="", description="One short sentence.")

    @property
    def score(self) -> float:
        if self.claims == 0:
            return 1.0
        return max(0.0, min(1.0, self.supported / self.claims))


MAX_SOURCE_CHARS = 1500


def _source_text(source: dict[str, Any]) -> str:
    content = source.get("content")
    text = content if isinstance(content, str) else json.dumps(mask_data(content), default=str)
    return f"[{source.get('kind', 'source')}: {source.get('name', '')}]\n{mask_text(text)[:MAX_SOURCE_CHARS]}"


def groundedness_messages(answer: AnswerOut) -> list[SystemMessage | HumanMessage]:
    sources = "\n\n".join(_source_text(s) for s in answer.sources) or "(no sources)"
    return [
        SystemMessage(content=prompts.GROUNDEDNESS_SYSTEM),
        HumanMessage(content=prompts.GROUNDEDNESS_USER.format(sources=sources, answer=mask_text(answer.text))),
    ]


async def judge_answer(llm: LLMProvider, answer: AnswerOut) -> GroundednessVerdict | None:
    try:
        return await invoke_structured(llm.get(ModelAlias.JUDGE), GroundednessVerdict, groundedness_messages(answer))
    except StructuredOutputError:
        return None


async def groundedness(llm: LLMProvider, output: ItemOutput) -> Score | None:
    """Mean judged groundedness of the assistant's answers in the item (None if it sent
    no answers). Judge failures are left out; all failing is a 0."""
    answers: Sequence[AnswerOut] = output.answers
    if not answers:
        return None
    values: list[float] = []
    unsupported: list[str] = []
    failed = 0
    for answer in answers:
        verdict = await judge_answer(llm, answer)
        if verdict is None:
            failed += 1
            continue
        values.append(verdict.score)
        unsupported += verdict.unsupported_claims
    if not values:
        return Score("groundedness", 0.0, f"the judge failed on all {failed} answer(s)")
    comment = f"{len(values)} answer(s)"
    if unsupported:
        comment += "; unsupported: " + "; ".join(mask_text(c) for c in unsupported[:5])
    if failed:
        comment += f"; judge failed on {failed}"
    return Score("groundedness", sum(values) / len(values), comment)


AsyncScorer = Callable[[LLMProvider, ItemOutput], Awaitable[Score | None]]
JUDGE_SCORERS: tuple[AsyncScorer, ...] = (groundedness,)

# Every score name a scorer can produce (each scorer is named after its score), and the
# ones the gate may set a threshold on.
SCORE_NAMES = frozenset(f.__name__ for f in (*DETERMINISTIC_SCORERS, *JUDGE_SCORERS))
GATED_SCORES = SCORE_NAMES - TRACKED_ONLY


async def score_item(output: ItemOutput, expected: Expected, llm: LLMProvider | None) -> list[Score]:
    """Every scorer that applies; the judge only when an LLM provider is given."""
    scores = [s for scorer in DETERMINISTIC_SCORERS if (s := scorer(output, expected)) is not None]
    if llm is not None:
        for judge in JUDGE_SCORERS:
            score = await judge(llm, output)
            if score is not None:
                scores.append(score)
    return scores
