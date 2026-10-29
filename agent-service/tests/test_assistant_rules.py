"""The handoff rules as plain code: each rule on its own, against real signal snapshots."""

import pytest

from assistant.rules import HandoffReason, evaluate_handoff_rules, said_not_helpful
from core.banking import BankingClient
from core.enums import IssueType
from core.signals import SignalSnapshot, build_signal_snapshot
from core.triage import TriageResult
from tests.banking_mock import ALICE, BOB


def _triage(category: IssueType, confidence: float = 0.95, threshold: float = 0.6) -> TriageResult:
    below = confidence < threshold
    return TriageResult(
        category=IssueType.OTHER if below else category,
        confidence=confidence,
        secondary_category=None,
        reasoning="facts",
        model_category=category,
        below_threshold=below,
    )


@pytest.fixture
async def alice(banking: BankingClient, clock) -> SignalSnapshot:
    return await build_signal_snapshot(banking, ALICE, clock=clock)


@pytest.fixture
async def bob(banking: BankingClient, clock) -> SignalSnapshot:
    return await build_signal_snapshot(banking, BOB, clock=clock)


def test_no_rule_fires_for_a_card_the_customer_froze(alice: SignalSnapshot) -> None:
    decision = evaluate_handoff_rules(
        "why was my card declined?", _triage(IssueType.CARD_DECLINE), alice, not_helpful_count=0
    )
    assert not decision.handoff and decision.reasons == []


@pytest.mark.parametrize(
    "message",
    [
        "Can I talk to someone?",
        "I want to speak to a real person",
        "can i chat with an agent please",
        "Please connect me to customer service",
        "human please",
        "agent",
        "Can someone call me back?",
    ],
)
def test_human_requested(alice: SignalSnapshot, message: str) -> None:
    decision = evaluate_handoff_rules(message, _triage(IssueType.CARD_DECLINE), alice, not_helpful_count=0)
    assert decision.primary is HandoffReason.HUMAN_REQUESTED


@pytest.mark.parametrize(
    "message",
    [
        "I think this is fraud",
        "someone used my card without asking",
        "there's a payment I didn't make",
        "I want to make a complaint",
        "I'd like to raise a formal complaint about this",
        "my mum passed away last week and her direct debit bounced",
        "I'm struggling to cope with all of this",
    ],
)
def test_sensitive_topic(alice: SignalSnapshot, message: str) -> None:
    decision = evaluate_handoff_rules(message, _triage(IssueType.CARD_DECLINE), alice, not_helpful_count=0)
    assert HandoffReason.SENSITIVE_TOPIC in decision.reasons


def test_a_customer_marked_vulnerable_is_sensitive(alice: SignalSnapshot) -> None:
    snapshot = alice.model_copy(update={"customer_vulnerable": True})
    decision = evaluate_handoff_rules(
        "why was my card declined", _triage(IssueType.CARD_DECLINE), snapshot, not_helpful_count=0
    )
    assert decision.primary is HandoffReason.SENSITIVE_TOPIC


def test_fraud_block_needs_a_human_action(bob: SignalSnapshot) -> None:
    for message in ("why was my card declined?", "Don't hand me off, I just want you to explain it yourself!"):
        decision = evaluate_handoff_rules(message, _triage(IssueType.CARD_DECLINE), bob, not_helpful_count=0)
        assert decision.reasons == [HandoffReason.NEEDS_HUMAN_ACTION]
        assert any("security check" in d for d in decision.details)


def test_needs_human_action_follows_the_category(bob: SignalSnapshot) -> None:
    """A fraud block on the card does not make a general question a handoff."""
    decision = evaluate_handoff_rules("what are your fees?", _triage(IssueType.OTHER), bob, not_helpful_count=0)
    assert not decision.handoff


def test_refund_decisions_need_a_human(alice: SignalSnapshot) -> None:
    decision = evaluate_handoff_rules(
        "can I get a refund for the failed payment fee?", _triage(IssueType.MISSED_PAYMENT), alice, not_helpful_count=0
    )
    assert decision.primary is HandoffReason.NEEDS_HUMAN_ACTION


@pytest.mark.parametrize(
    ("category", "field", "change", "detail"),
    [
        (IssueType.MISSED_PAYMENT, "failed_payments", {"failure_reason": "PAYEE_ACCOUNT_CLOSED"}, "recipient"),
        (
            IssueType.FAILED_DIRECT_DEBIT,
            "failed_collections",
            {"failure_code": "ACCOUNT_CLOSED_BY_ORIGINATOR"},
            "company closed",
        ),
    ],
)
def test_failures_only_ops_can_fix(
    alice: SignalSnapshot, category: IssueType, field: str, change: dict, detail: str
) -> None:
    items = [i.model_copy(update=change) for i in getattr(alice, field)]
    snapshot = alice.model_copy(update={field: items})
    decision = evaluate_handoff_rules("it didn't go out", _triage(category), snapshot, not_helpful_count=0)
    assert decision.reasons == [HandoffReason.NEEDS_HUMAN_ACTION]
    assert any(detail in d for d in decision.details)


def test_self_serve_failures_do_not_need_a_human(alice: SignalSnapshot) -> None:
    for category in (IssueType.MISSED_PAYMENT, IssueType.FAILED_DIRECT_DEBIT):  # insufficient funds, mandate cancelled
        assert not evaluate_handoff_rules("it didn't go out", _triage(category), alice, not_helpful_count=0).handoff


def test_an_inactive_account_needs_a_human(alice: SignalSnapshot) -> None:
    accounts = [a.model_copy(update={"status": "frozen"}) for a in alice.accounts]
    snapshot = alice.model_copy(update={"accounts": accounts})
    decision = evaluate_handoff_rules(
        "rent didn't go out", _triage(IssueType.MISSED_PAYMENT), snapshot, not_helpful_count=0
    )
    assert decision.primary is HandoffReason.NEEDS_HUMAN_ACTION


def test_low_triage_confidence(alice: SignalSnapshot) -> None:
    decision = evaluate_handoff_rules(
        "hmm", _triage(IssueType.CARD_DECLINE, confidence=0.4), alice, not_helpful_count=0
    )
    assert decision.reasons == [HandoffReason.LOW_CONFIDENCE]


def test_facts_that_do_not_explain_the_problem_are_low_confidence(bob: SignalSnapshot) -> None:
    decision = evaluate_handoff_rules(
        "my standing order didn't go out", _triage(IssueType.MISSED_PAYMENT), bob, not_helpful_count=0
    )
    assert decision.reasons == [HandoffReason.LOW_CONFIDENCE]


def test_incomplete_facts_are_not_held_against_the_customer(bob: SignalSnapshot) -> None:
    snapshot = bob.model_copy(update={"errors": ["payments"]})
    decision = evaluate_handoff_rules(
        "my standing order didn't go out", _triage(IssueType.MISSED_PAYMENT), snapshot, not_helpful_count=0
    )
    assert not decision.handoff


def test_repeated_failure(alice: SignalSnapshot) -> None:
    triage = _triage(IssueType.CARD_DECLINE)
    assert not evaluate_handoff_rules("why?", triage, alice, not_helpful_count=1).handoff
    decision = evaluate_handoff_rules("why?", triage, alice, not_helpful_count=2)
    assert decision.reasons == [HandoffReason.REPEATED_FAILURE]


def test_rules_are_reported_in_table_order(bob: SignalSnapshot) -> None:
    decision = evaluate_handoff_rules(
        "This is fraud, let me talk to a person", _triage(IssueType.CARD_DECLINE), bob, not_helpful_count=3
    )
    assert decision.reasons == [
        HandoffReason.HUMAN_REQUESTED,
        HandoffReason.SENSITIVE_TOPIC,
        HandoffReason.NEEDS_HUMAN_ACTION,
        HandoffReason.REPEATED_FAILURE,
    ]


def test_without_a_snapshot_the_words_still_count() -> None:
    decision = evaluate_handoff_rules("talk to a human", _triage(IssueType.CARD_DECLINE), None, not_helpful_count=0)
    assert decision.primary is HandoffReason.HUMAN_REQUESTED


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("That didn't help", True),
        ("this doesn't help at all", True),
        ("not helpful", True),
        ("it's still declined", True),
        ("my direct debit didn't work", False),
        ("thanks, that helps", False),
        ("why was my card declined?", False),
    ],
)
def test_said_not_helpful(message: str, expected: bool) -> None:
    assert said_not_helpful(message) is expected


@pytest.mark.parametrize(
    "message",
    [
        "why was my card declined at Tesco?",
        "my rent didn't go out",
        "the gym says my direct debit failed",
        "how do I unfreeze my card?",
        "what is my balance?",
    ],
)
def test_ordinary_questions_fire_no_word_rule(alice: SignalSnapshot, message: str) -> None:
    decision = evaluate_handoff_rules(message, _triage(IssueType.OTHER), alice, not_helpful_count=0)
    assert not decision.handoff
