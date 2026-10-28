"""Triage: structured output from the `triage` alias, grounded by the snapshot."""

from core.banking import BankingClient
from core.enums import IssueType
from core.prompts import triage as triage_prompt
from core.signals import build_signal_snapshot
from core.triage import TriageOutput, triage
from tests.banking_mock import ALICE
from tests.fakes import FakeLLMProvider, structured, structured_as_text, text


def _out(category: IssueType, confidence: float, secondary: IssueType | None = None) -> TriageOutput:
    return TriageOutput(category=category, confidence=confidence, secondary_category=secondary, reasoning="facts")


async def test_triage_returns_the_category(banking: BankingClient, clock) -> None:
    snapshot = await build_signal_snapshot(banking, ALICE, clock=clock)
    llm = FakeLLMProvider({"triage": [structured(_out(IssueType.FAILED_DIRECT_DEBIT, 0.9, IssueType.MISSED_PAYMENT))]})
    result = await triage(llm, "my rent didn't go out", snapshot, threshold=0.6)
    assert result.category is IssueType.FAILED_DIRECT_DEBIT
    assert result.secondary_category is IssueType.MISSED_PAYMENT
    assert result.confidence == 0.9 and not result.below_threshold
    assert result.prompt_version == triage_prompt.PROMPT_VERSION

    call = llm.fake("triage").calls[0]
    assert call.tool_names == ["TriageOutput"]
    # Grounded by facts: the prompt carries the snapshot with reason codes.
    assert "MANDATE_CANCELLED" in call.text and "CARD_FROZEN" in call.text
    assert "my rent didn't go out" in call.text
    # Only the triage alias was used.
    assert all(not llm.fake(a).calls for a in ("assistant", "reasoning", "simulator", "judge"))


async def test_low_confidence_returns_other() -> None:
    llm = FakeLLMProvider({"triage": [structured(_out(IssueType.CARD_DECLINE, 0.4))]})
    result = await triage(llm, "something is wrong", None, threshold=0.6)
    assert result.category is IssueType.OTHER
    assert result.model_category is IssueType.CARD_DECLINE and result.below_threshold
    assert "not available" in llm.fake("triage").calls[0].text


async def test_threshold_is_inclusive() -> None:
    llm = FakeLLMProvider({"triage": [structured(_out(IssueType.CARD_DECLINE, 0.6))]})
    assert (await triage(llm, "declined", None, threshold=0.6)).category is IssueType.CARD_DECLINE


async def test_json_in_text_is_accepted() -> None:
    llm = FakeLLMProvider({"triage": [structured_as_text(_out(IssueType.MISSED_PAYMENT, 0.8))]})
    assert (await triage(llm, "standing order", None, threshold=0.6)).category is IssueType.MISSED_PAYMENT


async def test_unusable_output_falls_back_to_other() -> None:
    llm = FakeLLMProvider({"triage": [text("no idea")]})
    result = await triage(llm, "help", None, threshold=0.6)
    assert result.category is IssueType.OTHER and result.confidence == 0.0 and result.model_category is None


async def test_complaint_is_masked_before_the_model() -> None:
    llm = FakeLLMProvider({"triage": [structured(_out(IssueType.CARD_DECLINE, 0.9))]})
    await triage(llm, "card 4111 1111 1111 1111 declined, acct 12345678", None, threshold=0.6)
    sent = llm.fake("triage").calls[0].text
    assert "4111 1111" not in sent and "12345678" not in sent
