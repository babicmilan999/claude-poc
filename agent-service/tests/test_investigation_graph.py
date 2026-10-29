"""The investigation graph (architecture section 6) with the fake LLM, the in-memory
checkpointer and the banking API mocked from the contract: every route, verify, park,
the evidence-check loop, escalation, step budgets and restricted toolsets."""

import json

import pytest

from core.banking import BankingClient
from core.enums import CaseSource, CaseStatus, InvestigationStatus, InvestigationTrigger, IssueType, RootCause
from core.tools import OPS_TOOLSETS
from investigation.state import INVESTIGATOR_NODES
from tests.banking_mock import ALICE, ALICE_SCHEDULED, BOB, ContractBankingMock
from tests.fakes import FakeLLMProvider, text, tool_call, tool_calls
from tests.investigation.harness import diagnosis, enough, make_harness, not_enough, triaged


@pytest.fixture
def llm() -> FakeLLMProvider:
    return FakeLLMProvider()


async def test_card_decline_from_email_is_triaged_investigated_and_diagnosed(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="my card keeps getting declined at Tesco")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_calls(("get_cards", {}), ("get_card_authorisations", {"result": "declined"})),
        text("The card is frozen by the customer (ev-1); both declines are CARD_FROZEN (ev-2)."),
        enough("ev-1", "ev-2"),
        diagnosis(
            IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1", "ev-2", action="Explain how to unfreeze."
        ),
        text("The customer froze the card ending 4242 (ev-1), so Tesco payments were declined (ev-2)."),
    )

    assert await h.service.run_pending() == 1

    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.AWAITING_REVIEW
    assert h.store.cases[case_id].status == CaseStatus.AWAITING_REVIEW
    assert h.store.cases[case_id].category == IssueType.CARD_DECLINE
    assert inv.issue_type == IssueType.CARD_DECLINE and inv.steps_used == 2
    assert inv.diagnosis["root_cause"] == RootCause.CARD_FROZEN_BY_CUSTOMER
    assert inv.diagnosis["evidence_ids"] == ["ev-1", "ev-2"]
    assert "ev-1" in inv.diagnosis["explanation"]
    assert [e["tool"] for e in inv.evidence] == ["get_cards", "get_card_authorisations"]
    assert inv.evidence[0]["origin"] == "investigation" and "FROZEN_BY_CUSTOMER" in inv.evidence[0]["summary"]

    assert h.nodes(inv.id) == ["intake", "triage", "route", "card_decline_investigator", "evidence_check", "diagnose"]
    types = h.event_types(inv.id)
    assert types[0] == "status_changed" and h.events(inv.id)[0].payload["status"] == "running"
    assert types.count("tool_call") == 2 and types.count("evidence") == 2
    assert "token" in types and types.index("token") < types.index("diagnosis")
    assert types[-2:] == ["awaiting_review", "status_changed"]
    classified = next(e for e in h.events(inv.id) if e.type == "classified")
    assert classified.payload["method"] == "triaged" and classified.payload["issue_type"] == "card_decline"
    tokens = "".join(e.payload["text"] for e in h.events(inv.id) if e.type == "token")
    assert tokens.strip() == inv.diagnosis["explanation"]


@pytest.mark.parametrize(
    ("issue_type", "customer", "calls", "evidence_ids", "root_cause"),
    [
        (
            IssueType.MISSED_PAYMENT,
            ALICE,
            [("get_scheduled_payments", {}), ("get_payment_attempts", {"scheduled_payment_id": ALICE_SCHEDULED})],
            ["ev-2"],
            RootCause.INSUFFICIENT_FUNDS,
        ),
        (
            IssueType.FAILED_DIRECT_DEBIT,
            ALICE,
            [("get_direct_debit_collections", {"result": "failed"}), ("get_mandates", {})],
            ["ev-1", "ev-2"],
            RootCause.MANDATE_CANCELLED_BY_CUSTOMER,
        ),
        (
            IssueType.CARD_DECLINE,
            BOB,
            [("get_card_authorisations", {"result": "declined"}), ("get_fraud_flags", {})],
            ["ev-1", "ev-2"],
            RootCause.FRAUD_RULE_TRIGGERED,
        ),
    ],
    ids=["missed_payment", "failed_direct_debit", "card_decline_fraud"],
)
async def test_each_issue_type_routes_to_its_investigator_with_only_its_tools(
    llm: FakeLLMProvider,
    banking: BankingClient,
    issue_type: IssueType,
    customer: str,
    calls: list[tuple[str, dict]],
    evidence_ids: list[str],
    root_cause: RootCause,
) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=customer, complaint="something went wrong with a payment")
    llm.script("triage", triaged(issue_type))
    llm.script(
        "reasoning",
        tool_calls(*calls),
        text("Found it."),
        enough(*evidence_ids),
        diagnosis(issue_type, root_cause, *evidence_ids),
        text("Explanation."),
    )
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.AWAITING_REVIEW
    assert INVESTIGATOR_NODES[issue_type] in h.nodes(inv.id)
    investigator_calls = llm.fake("reasoning").calls[:2]
    for call in investigator_calls:
        assert call.tool_names == list(OPS_TOOLSETS[issue_type])
    assert inv.diagnosis["root_cause"] == root_cause


async def test_generic_investigator_on_a_manual_run_of_an_unclear_case(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    """`other` is not auto-investigated; a manual run (Investigate) is never parked and
    goes to the generic investigator with the union toolset and a smaller budget."""
    h = make_harness(llm, banking, generic_investigator_step_budget=2)
    case_id = h.store.add_case(
        customer_id=ALICE, complaint="something is off with my account", trigger=InvestigationTrigger.MANUAL
    )
    llm.script("triage", triaged(IssueType.OTHER, 0.3))
    llm.script(
        "reasoning",
        tool_call("get_account_balance", {}),
        tool_call("get_transactions", {}),  # the last allowed step; the budget ends the pass
        enough("ev-1"),
        diagnosis(IssueType.OTHER, RootCause.UNKNOWN, "ev-1", confidence=0.3),
        text("Nothing failed."),
    )
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert "generic_investigator" in h.nodes(inv.id)
    assert inv.steps_used == 2
    assert set(llm.fake("reasoning").calls[0].tool_names) == set(OPS_TOOLSETS[IssueType.OTHER])
    assert "last step" in llm.fake("reasoning").calls[1].text
    assert inv.status == InvestigationStatus.AWAITING_REVIEW


async def test_auto_run_of_a_type_not_enabled_is_parked(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="I want to change my address")
    llm.script("triage", triaged(IssueType.OTHER, 0.9))

    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.COMPLETED and inv.finished_at is not None
    assert inv.diagnosis is None and inv.escalation is None
    assert h.store.cases[case_id].status == CaseStatus.NEEDS_MANUAL
    assert h.nodes(inv.id) == ["intake", "triage", "route", "park"]
    assert h.events(inv.id)[-1].payload["status"] == "completed"
    assert not llm.fake("reasoning").calls, "a parked run calls no investigator"


async def test_auto_investigate_types_come_from_settings(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking, auto_types=frozenset({IssueType.CARD_DECLINE}))
    case_id = h.store.add_case(customer_id=ALICE, complaint="my rent did not go out")
    llm.script("triage", triaged(IssueType.MISSED_PAYMENT))
    await h.service.run_pending()
    assert h.store.cases[case_id].status == CaseStatus.NEEDS_MANUAL


async def test_handoff_category_confirmed_by_signals_skips_triage(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(
        customer_id=ALICE,
        source=CaseSource.ASSISTANT_HANDOFF,
        category=IssueType.FAILED_DIRECT_DEBIT,
        summary="Customer says the letting agent could not collect the rent.",
        handoff_evidence=[
            {
                "id": "ev-1",
                "origin": "assistant",
                "tool": "get_my_direct_debits",
                "summary": "Direct Debit to Acme Lettings failed",
                "data": [{"originator": "Acme Lettings", "status": "failed"}],
                "created_at": "2026-10-28T10:00:00Z",
            }
        ],
        transcript=[("customer", "the gym says my direct debit failed"), ("assistant", "Let me hand you over.")],
    )
    llm.script(
        "reasoning",
        tool_calls(("get_direct_debit_collections", {"result": "failed"}), ("get_mandates", {})),
        text("Mandate cancelled by the customer (ev-3)."),
        enough("ev-2", "ev-3"),
        diagnosis(IssueType.FAILED_DIRECT_DEBIT, RootCause.MANDATE_CANCELLED_BY_CUSTOMER, "ev-1", "ev-3"),
        text("Explanation."),
    )
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert not llm.fake("triage").calls, "verify does not run a full triage"
    assert h.nodes(inv.id)[:3] == ["intake", "verify", "route"]
    classified = next(e for e in h.events(inv.id) if e.type == "classified")
    assert (
        classified.payload["method"] == "verified" and classified.payload["previous_category"] == "failed_direct_debit"
    )
    assert inv.category_changed is False
    # The assistant's evidence is kept and citable; investigation evidence continues the ids.
    assert [(e["id"], e["origin"]) for e in inv.evidence] == [
        ("ev-1", "assistant"),
        ("ev-2", "investigation"),
        ("ev-3", "investigation"),
    ]
    assert inv.diagnosis["evidence_ids"] == ["ev-1", "ev-3"]
    assert "letting agent" in llm.fake("reasoning").calls[0].text


async def test_doubtful_handoff_category_falls_back_to_triage(llm: FakeLLMProvider, banking: BankingClient) -> None:
    """Bob has a fraud decline and nothing else: a `missed_payment` handoff does not hold up."""
    h = make_harness(llm, banking)
    case_id = h.store.add_case(
        customer_id=BOB,
        source=CaseSource.ASSISTANT_HANDOFF,
        category=IssueType.MISSED_PAYMENT,
        summary="payment failed",
    )
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_fraud_flags", {}),
        text("Fraud rule FR-12 (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.FRAUD_RULE_TRIGGERED, "ev-1"),
        text("Explanation."),
    )
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert h.nodes(inv.id)[:4] == ["intake", "verify", "triage", "route"]
    assert inv.category_changed is True
    assert h.store.cases[case_id].category == IssueType.CARD_DECLINE
    classified = next(e for e in h.events(inv.id) if e.type == "classified")
    assert classified.payload["previous_category"] == "missed_payment"


async def test_not_enough_evidence_with_budget_left_loops_back_to_triage(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="my payment did not work")
    llm.script("triage", triaged(IssueType.MISSED_PAYMENT), triaged(IssueType.FAILED_DIRECT_DEBIT))
    llm.script(
        "reasoning",
        tool_call("get_account_balance", {}),
        text("Balance is fine (ev-1)."),
        not_enough("Nothing failed on the scheduled payments side."),
        tool_call("get_direct_debit_collections", {"result": "failed"}),
        text("Collection failed MANDATE_CANCELLED (ev-2)."),
        enough("ev-2"),
        diagnosis(IssueType.FAILED_DIRECT_DEBIT, RootCause.MANDATE_CANCELLED_BY_CUSTOMER, "ev-2"),
        text("Explanation."),
    )
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert h.nodes(inv.id) == [
        "intake",
        "triage",
        "route",
        "missed_payment_investigator",
        "evidence_check",
        "triage",
        "route",
        "failed_direct_debit_investigator",
        "evidence_check",
        "diagnose",
    ]
    # The second triage sees the first pass's findings.
    assert "Balance is fine" in llm.fake("triage").calls[1].text
    assert inv.issue_type == IssueType.FAILED_DIRECT_DEBIT and inv.steps_used == 4


async def test_budget_spent_without_a_conclusion_escalates(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking, investigation_max_steps=3, investigator_step_budget=2)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE), triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("Card is frozen, not sure why (ev-1)."),
        not_enough("Who froze the card?"),
        tool_call("get_card_controls", {"card_id": "c1c1c1c1-0000-4000-8000-000000000001"}),
        not_enough("Still unclear."),
    )
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert inv.status == InvestigationStatus.AWAITING_REVIEW
    assert h.store.cases[case_id].status == CaseStatus.ESCALATED
    assert inv.diagnosis is None and inv.steps_used == 3
    assert inv.escalation["evidence_ids"] == ["ev-1", "ev-2"]
    assert "Still unclear." in inv.escalation["summary"] and "3 of 3 steps" in inv.escalation["summary"]
    nodes = h.nodes(inv.id)
    assert nodes[-1] == "escalate" and "diagnose" not in nodes
    types = h.event_types(inv.id)
    assert "escalated" in types and "diagnosis" not in types
    assert next(e for e in h.events(inv.id) if e.type == "awaiting_review").payload == {"has_diagnosis": False}


async def test_max_passes_escalates_even_with_steps_left(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking, investigation_max_passes=1)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script("reasoning", tool_call("get_cards", {}), text("Frozen."), not_enough())
    await h.service.run_pending()
    assert h.store.cases[case_id].status == CaseStatus.ESCALATED


async def test_the_evidence_check_gates_the_diagnosis(llm: FakeLLMProvider, banking: BankingClient) -> None:
    """No tool evidence: no LLM evidence check, no diagnosis, however sure the model is."""
    h = make_harness(llm, banking, investigation_max_passes=1)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script("reasoning", text("It is obviously the fraud rule."))
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert inv.diagnosis is None and inv.escalation is not None
    assert len(llm.fake("reasoning").calls) == 1


async def test_evidence_check_citing_only_unknown_ids_is_not_enough(
    llm: FakeLLMProvider, banking: BankingClient
) -> None:
    h = make_harness(llm, banking, investigation_max_passes=1)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script("reasoning", tool_call("get_cards", {}), text("Frozen."), enough("ev-99"))
    await h.service.run_pending()
    assert h.store.cases[case_id].status == CaseStatus.ESCALATED


async def test_diagnosis_citations_are_limited_to_known_evidence(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("Frozen (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-7"),
        text("Explanation."),
    )
    await h.service.run_pending()
    assert h.only_investigation(case_id).diagnosis["evidence_ids"] == ["ev-1"]


async def test_tools_outside_the_toolset_are_refused(
    llm: FakeLLMProvider, banking: BankingClient, banking_mock: ContractBankingMock
) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(customer_id=ALICE, complaint="my rent did not go out")
    llm.script("triage", triaged(IssueType.MISSED_PAYMENT))
    llm.script(
        "reasoning",
        tool_calls(("get_fraud_flags", {}), ("get_scheduled_payments", {"customer_id": BOB})),
        tool_call("get_scheduled_payments", {}),
        text("Failed for insufficient funds (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.MISSED_PAYMENT, RootCause.INSUFFICIENT_FUNDS, "ev-1"),
        text("Explanation."),
    )
    before = len(banking_mock.operations_called)
    await h.service.run_pending()

    inv = h.only_investigation(case_id)
    assert [e["tool"] for e in inv.evidence] == ["get_scheduled_payments"]
    replies = [m for m in llm.fake("reasoning").calls[1].messages if m.type == "tool"]
    assert "not available to this investigator" in str(replies[0].content)
    assert "customer_id" in str(replies[1].content)  # rejected: tools never take a customer id
    called = banking_mock.operations_called[before:]
    assert "listFraudFlags" not in called
    assert all(BOB not in r.url.path for r in banking_mock.requests)
    assert {r.method for r in banking_mock.requests} == {"GET"}


async def test_complaint_and_tool_data_are_masked_before_the_llm(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    h.store.add_case(
        customer_id=ALICE, complaint="card 4111 1111 1111 1111 declined, my account 12345678, sort code 12-34-56"
    )
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_account_balance", {}),
        text("ok"),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.INSUFFICIENT_FUNDS, "ev-1"),
        text("Explanation."),
    )
    await h.service.run_pending()
    sent = h.llm_text()
    assert "4111 1111 1111 1111" not in sent and "12345678" not in sent and "12-34-56" not in sent
    stored = json.dumps([e.payload for e in h.store.events], default=str)
    assert "4111 1111 1111 1111" not in stored and "12345678" not in stored


async def test_answer_keys_never_reach_the_graph(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    case_id = h.store.add_case(
        customer_id=ALICE,
        complaint="card declined",
        simulated=True,
        expected_root_cause="card_frozen_by_customer",
        expected_outcome="self_serve",
    )
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("ok"),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1"),
        text("Explanation."),
    )
    await h.service.run_pending()
    inv = h.only_investigation(case_id)
    snapshot = await h.service.graph.aget_state({"configurable": {"thread_id": str(inv.id)}})
    surfaces = [h.llm_text(), json.dumps(snapshot.values, default=str), json.dumps([e.payload for e in h.store.events])]
    for surface in surfaces:
        assert "self_serve" not in surface and "expected_" not in surface
    intake = await h.store.load_intake(case_id)
    assert not any(name.startswith("expected") for name in vars(intake))


def test_review_cannot_be_skipped_and_evidence_gates_diagnosis(llm: FakeLLMProvider, banking: BankingClient) -> None:
    h = make_harness(llm, banking)
    edges = h.service.graph.get_graph().edges
    into: dict[str, set[str]] = {}
    for edge in edges:
        into.setdefault(edge.target, set()).add(edge.source)
    assert into["finalize"] == {"review"}
    assert into["diagnose"] == {"evidence_check"}
    assert into["review"] == {"diagnose", "escalate"}
    assert into["park"] == {"route"}
    assert into["evidence_check"] == set(INVESTIGATOR_NODES.values())
    assert {e.target for e in edges if e.source == "diagnose"} == {"review", "escalate"}
    assert {e.target for e in edges if e.source == "escalate"} == {"review"}
