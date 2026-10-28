"""Ops-scoped tools: full records, restricted per investigator, read-only, masked."""

import json

import pytest
from pydantic import ValidationError

from core.banking import BankingClient
from core.enums import IssueType
from core.tools import OPS_TOOLSETS, make_ops_tools, ops_tools_for
from tests.banking_mock import ALICE, ALICE_PAYEE, ALICE_SCHEDULED, BOB, ContractBankingMock


def _call(name: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": "call-1", "type": "tool_call"}


def test_toolsets_match_section_6() -> None:
    assert OPS_TOOLSETS[IssueType.CARD_DECLINE] == (
        "get_cards",
        "get_card_authorisations",
        "get_account_balance",
        "get_card_controls",
        "get_fraud_flags",
    )
    assert OPS_TOOLSETS[IssueType.MISSED_PAYMENT] == (
        "get_scheduled_payments",
        "get_payment_attempts",
        "get_account_balance",
        "get_transactions",
        "get_payee",
    )
    assert OPS_TOOLSETS[IssueType.FAILED_DIRECT_DEBIT] == (
        "get_mandates",
        "get_direct_debit_collections",
        "get_account_balance",
        "get_transactions",
    )
    union = set().union(
        *(OPS_TOOLSETS[t] for t in (IssueType.CARD_DECLINE, IssueType.MISSED_PAYMENT, IssueType.FAILED_DIRECT_DEBIT))
    )
    assert set(OPS_TOOLSETS[IssueType.OTHER]) == union


@pytest.mark.parametrize("issue_type", list(IssueType))
def test_investigator_gets_only_its_tools(banking: BankingClient, issue_type: IssueType) -> None:
    tools = ops_tools_for(banking, BOB, issue_type)
    assert [t.name for t in tools] == list(OPS_TOOLSETS[issue_type])


def test_ops_tools_are_bound_to_the_case_customer(banking: BankingClient) -> None:
    for tool in make_ops_tools(banking, BOB):
        assert "customer_id" not in tool.tool_call_schema.model_json_schema().get("properties", {})
    with pytest.raises(ValueError):
        make_ops_tools(banking, "")


async def test_ops_tools_return_full_records(banking: BankingClient, clock) -> None:
    tools = {t.name: t for t in make_ops_tools(banking, BOB, clock=clock)}
    flags = json.loads((await tools["get_fraud_flags"].ainvoke(_call("get_fraud_flags", {}))).content)
    assert flags[0]["rule_id"] == "FR-12" and flags[0]["risk_score"] == 0.93
    auths = json.loads(
        (await tools["get_card_authorisations"].ainvoke(_call("x", {"result": "declined", "since_days": 7}))).content
    )
    assert auths[0]["decline_reason"] == "FRAUD_RULE_TRIGGERED" and auths[0]["fraud_rule_id"] == "FR-12"
    cards = json.loads((await tools["get_cards"].ainvoke(_call("x", {}))).content)
    assert cards[0]["status_reason"] == "FRAUD_SUSPECTED"


async def test_ops_tools_mask_account_identifiers(banking: BankingClient, clock) -> None:
    tools = {t.name: t for t in make_ops_tools(banking, ALICE, clock=clock)}
    payee = (await tools["get_payee"].ainvoke(_call("get_payee", {"payee_id": ALICE_PAYEE}))).content
    assert "87654321" not in payee and "20-00-00" not in payee
    assert json.loads(payee)["account_status"] == "open"


async def test_ops_tool_filters_and_windows(banking: BankingClient, clock) -> None:
    tools = {t.name: t for t in make_ops_tools(banking, ALICE, clock=clock)}
    attempts = json.loads(
        (
            await tools["get_payment_attempts"].ainvoke(
                _call("x", {"scheduled_payment_id": ALICE_SCHEDULED, "result": ["failed", "skipped"]})
            )
        ).content
    )
    assert attempts[0]["failure_reason"] == "INSUFFICIENT_FUNDS" and attempts[0]["retry_count"] == 2
    collections = json.loads(
        (await tools["get_direct_debit_collections"].ainvoke(_call("x", {"result": "failed"}))).content
    )
    assert [c["failure_code"] for c in collections] == ["MANDATE_CANCELLED"]
    recent = json.loads((await tools["get_transactions"].ainvoke(_call("x", {"since_days": 10}))).content)
    assert [t["description"] for t in recent] == ["Coffee House"]
    balances = json.loads((await tools["get_account_balance"].ainvoke(_call("x", {}))).content)
    assert balances[0]["available"] == "250.00" and balances[0]["account_status"] == "active"


async def test_bad_arguments_are_rejected(banking: BankingClient) -> None:
    tools = {t.name: t for t in make_ops_tools(banking, ALICE)}
    with pytest.raises(ValidationError):
        await tools["get_card_authorisations"].ainvoke(_call("x", {"result": "maybe"}))
    with pytest.raises(ValidationError):
        await tools["get_cards"].ainvoke(_call("x", {"customer_id": BOB}))


async def test_ops_tools_are_read_only(banking: BankingClient, banking_mock: ContractBankingMock, clock) -> None:
    for tool in make_ops_tools(banking, ALICE, clock=clock):
        args = {"card_id": "c1c1c1c1-0000-4000-8000-000000000001"} if tool.name == "get_card_controls" else {}
        if tool.name == "get_payee":
            args = {"payee_id": ALICE_PAYEE}
        await tool.ainvoke(_call(tool.name, args))
    assert {r.method for r in banking_mock.requests} == {"GET"}
    assert all(r.url.path.startswith(f"/customers/{ALICE}") for r in banking_mock.requests)
