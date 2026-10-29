"""Simulated and scripted conversations through `AssistantService.run_turn` (the customer
app's path), with the assistant graph on the fake LLM and the contract banking mock."""

from typing import Any

import pytest

from core.banking import BankingClient
from core.enums import ConversationOutcome, ConversationStatus, ExpectedOutcome, IssueType, RootCause
from core.kb import KnowledgeBaseSearch
from simulation.journey import reply_from_events, run_scripted_conversation, run_simulated_conversation
from simulation.persona import CustomerTurn, CustomerView, PersonaSpec, SimulatedCustomer
from tests.assistant.helpers import CARD_ANSWER, Harness, classified, input_ok, judge
from tests.banking_mock import ALICE, BOB
from tests.fakes import FakeLLMProvider, structured, text, tool_call


@pytest.fixture
def h(fake_llm: FakeLLMProvider, banking: BankingClient, kb: KnowledgeBaseSearch) -> Harness:
    return Harness(fake_llm, banking, kb)


def customer(h: Harness, customer_id: str, max_turns: int = 6) -> SimulatedCustomer:
    view = CustomerView(
        customer_id=customer_id,  # type: ignore[arg-type]
        customer_name="Test",
        persona=PersonaSpec(knows=["the card was declined today"], max_turns=max_turns),
    )
    return SimulatedCustomer(h.llm, view)


def says(message: str) -> object:
    return structured(CustomerTurn(message=message))


def done(satisfied: bool) -> object:
    return structured(CustomerTurn(done=True, satisfied=satisfied))


def script_card_answer(h: Harness) -> None:
    h.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    h.script("assistant", tool_call("get_my_cards"), text(CARD_ANSWER))
    h.script("judge", judge(True))


async def test_resolved_when_the_customer_is_satisfied(h: Harness) -> None:
    conversation = await h.service.create_conversation(
        ALICE,
        simulated=True,
        expected_outcome=ExpectedOutcome.SELF_SERVE,
        expected_root_cause=RootCause.CARD_FROZEN_BY_CUSTOMER,
    )
    h.script("simulator", says("Why was my card declined at Tesco?"), done(True))
    script_card_answer(h)

    result = await run_simulated_conversation(
        h.service, conversation, customer(h, ALICE), max_turns=6, capture_answers=True
    )

    assert result.outcome is ConversationOutcome.RESOLVED and result.ended_by == "customer_satisfied"
    assert result.turns == 1 and result.case_id is None
    assert [line.role for line in result.lines] == ["customer", "assistant"]
    assert result.lines[1].text == CARD_ANSWER
    # The answer and the sources the output guard checked, for the groundedness judge.
    (answer,) = result.answers
    assert answer.text == CARD_ANSWER and any(s["name"] == "get_my_cards" for s in answer.sources)
    stored = await h.store.get_conversation(conversation.id)
    assert stored is not None and stored.status is ConversationStatus.CLOSED and stored.simulated
    # The simulator saw the assistant's answer before deciding it was done.
    assert CARD_ANSWER in h.llm.fake("simulator").calls[1].text


async def test_handoff_ends_the_journey_and_the_case_keeps_the_answer_key(h: Harness) -> None:
    conversation = await h.service.create_conversation(
        BOB,
        simulated=True,
        expected_outcome=ExpectedOutcome.NEEDS_HUMAN,
        expected_root_cause=RootCause.FRAUD_RULE_TRIGGERED,
    )
    h.script("simulator", says("My card doesn't work anywhere since this morning."))
    h.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    h.script("assistant", text("Customer's card stopped working; blocked."))  # handoff summary

    result = await run_simulated_conversation(h.service, conversation, customer(h, BOB), max_turns=6)

    assert result.outcome is ConversationOutcome.HANDED_OFF and result.ended_by == "handoff"
    assert result.handed_off and result.case_id is not None
    assert "An agent will look into this" in result.lines[-1].text
    (case,) = h.store.cases
    assert case["id"] == result.case_id and case["simulated"] is True
    assert case["expected_outcome"] is ExpectedOutcome.NEEDS_HUMAN
    assert case["expected_root_cause"] is RootCause.FRAUD_RULE_TRIGGERED
    assert len(h.llm.fake("simulator").calls) == 1  # not asked again after the handoff


async def test_abandoned_when_turns_run_out(h: Harness) -> None:
    conversation = await h.service.create_conversation(ALICE, simulated=True)
    h.script("simulator", says("Why was my card declined at Tesco?"), says("Are you sure?"))
    script_card_answer(h)

    result = await run_simulated_conversation(h.service, conversation, customer(h, ALICE, max_turns=5), max_turns=1)

    assert result.outcome is ConversationOutcome.ABANDONED and result.ended_by == "max_turns" and result.turns == 1
    # The simulator wrote the one turn it could send and was not asked for a second.
    assert len(h.llm.fake("simulator").calls) == 1


async def test_persona_max_turns_also_stops_before_another_simulator_call(h: Harness) -> None:
    conversation = await h.service.create_conversation(ALICE, simulated=True)
    h.script("simulator", says("Why was my card declined at Tesco?"), says("Are you sure?"), says("Really?"))
    script_card_answer(h)
    h.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    h.script("assistant", tool_call("get_my_cards"), text(CARD_ANSWER))
    h.script("judge", judge(True))

    result = await run_simulated_conversation(h.service, conversation, customer(h, ALICE, max_turns=2), max_turns=6)

    assert result.ended_by == "max_turns" and result.turns == 2
    assert len(h.llm.fake("simulator").calls) == 2


async def test_abandoned_when_the_customer_gives_up(h: Harness) -> None:
    conversation = await h.service.create_conversation(ALICE, simulated=True)
    h.script("simulator", says("Why was my card declined at Tesco?"), done(False))
    script_card_answer(h)
    result = await run_simulated_conversation(h.service, conversation, customer(h, ALICE), max_turns=6)
    assert result.outcome is ConversationOutcome.ABANDONED and result.ended_by == "customer_gave_up"

    silent = await h.service.create_conversation(ALICE, simulated=True)
    h.script("simulator", done(True))  # leaves before saying anything: not "resolved"
    result = await run_simulated_conversation(h.service, silent, customer(h, ALICE), max_turns=6)
    assert result.outcome is ConversationOutcome.ABANDONED and result.turns == 0


async def test_scripted_conversation_runs_every_turn_then_abandons(h: Harness) -> None:
    conversation = await h.service.create_conversation(ALICE, simulated=True)
    script_card_answer(h)
    script_card_answer(h)
    result = await run_scripted_conversation(
        h.service, conversation, ["Why was my card declined?", "How do I unfreeze it?"], capture_answers=True
    )
    assert result.turns == 2 and len(result.answers) == 2
    assert result.outcome is ConversationOutcome.ABANDONED and result.ended_by == "script_end"
    assert h.llm.fake("simulator").calls == []  # scripts need no simulated customer


async def test_scripted_conversation_stops_at_a_handoff(h: Harness) -> None:
    conversation = await h.service.create_conversation(BOB, simulated=True)
    h.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    h.script("assistant", text("Card blocked."))
    result = await run_scripted_conversation(h.service, conversation, ["My card is blocked", "hello?"])
    assert result.ended_by == "handoff" and result.turns == 1 and result.outcome is ConversationOutcome.HANDED_OFF


def test_reply_from_events() -> None:
    events: list[dict[str, Any]] = [
        {"type": "message_created", "message": {"role": "customer", "content": "hi"}},
        {"type": "token", "message_id": 2, "text": "Hel"},
        {"type": "message_completed", "message": {"role": "assistant", "content": "Hello"}},
        {"type": "handoff", "case_id": "6f1c1c1c-0000-4000-8000-000000000001", "notice": "An agent will look"},
        {"type": "conversation_status", "status": "closed", "outcome": "handed_off"},
        {"type": "error", "message": "oops"},
    ]
    reply = reply_from_events(events)
    assert reply.text == "Hello" and reply.handed_off and reply.closed and reply.error == "oops"
    assert str(reply.case_id) == "6f1c1c1c-0000-4000-8000-000000000001"
