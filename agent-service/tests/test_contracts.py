"""The code against `contracts/`: shared enums, banking models, and the client covering
every read-only banking endpoint."""

import inspect
import typing
from enum import StrEnum
from types import UnionType
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from core import enums
from core.banking import models as bm
from core.banking.client import BankingClient
from tests.banking_mock import (
    ALICE,
    ALICE_ACCOUNT,
    ALICE_CARD,
    ALICE_PAYEE,
    ContractBankingMock,
    load_json,
    load_yaml,
)

SCENARIO = load_json("scenario.schema.json")
BANKING = load_yaml("banking-api.yaml")
AGENT = load_yaml("agent-api.yaml")


def _values(enum: type[StrEnum]) -> list[str]:
    return [e.value for e in enum]


@pytest.mark.parametrize(
    ("enum", "name"),
    [(enums.IssueType, "IssueType"), (enums.RootCause, "RootCause"), (enums.ExpectedOutcome, "ExpectedOutcome")],
)
def test_shared_enums_match_scenario_schema(enum: type[StrEnum], name: str) -> None:
    assert _values(enum) == SCENARIO["$defs"][name]["enum"]


@pytest.mark.parametrize(
    ("enum", "name"),
    [
        (enums.ConversationStatus, "ConversationStatus"),
        (enums.ConversationOutcome, "ConversationOutcome"),
        (enums.Rating, "Rating"),
        (enums.CaseSource, "CaseSource"),
        (enums.CaseStatus, "CaseStatus"),
        (enums.InvestigationStatus, "InvestigationStatus"),
        (enums.InvestigationTrigger, "InvestigationTrigger"),
        (enums.Verdict, "Verdict"),
    ],
)
def test_agent_enums_match_agent_api(enum: type[StrEnum], name: str) -> None:
    assert _values(enum) == AGENT["components"]["schemas"][name]["enum"]


def test_investigation_event_types_match_agent_api() -> None:
    mapping = AGENT["components"]["schemas"]["InvestigationEvent"]["discriminator"]["mapping"]
    assert set(_values(enums.InvestigationEventType)) == set(mapping)


def test_message_roles_and_evidence_origin_match_agent_api() -> None:
    schemas = AGENT["components"]["schemas"]
    assert _values(enums.MessageRole) == schemas["Message"]["properties"]["role"]["enum"]
    assert _values(enums.EvidenceOrigin) == schemas["Evidence"]["properties"]["origin"]["enum"]


BANKING_ENUMS = {
    "CustomerStatus": bm.CustomerStatus,
    "AccountStatus": bm.AccountStatus,
    "TransactionType": bm.TransactionType,
    "CardStatus": bm.CardStatus,
    "CardStatusReason": bm.CardStatusReason,
    "AuthorisationResult": bm.AuthorisationResult,
    "DeclineReason": bm.DeclineReason,
    "FraudFlagStatus": bm.FraudFlagStatus,
    "PaymentAttemptResult": bm.PaymentAttemptResult,
    "PaymentFailureReason": bm.PaymentFailureReason,
    "MandateStatus": bm.MandateStatus,
    "CollectionResult": bm.CollectionResult,
    "CollectionFailureCode": bm.CollectionFailureCode,
}


@pytest.mark.parametrize(("name", "enum"), BANKING_ENUMS.items())
def test_banking_enums_match_contract(name: str, enum: type[StrEnum]) -> None:
    assert _values(enum) == BANKING["components"]["schemas"][name]["enum"]


BANKING_MODELS: dict[str, type[BaseModel]] = {
    "Customer": bm.Customer,
    "Account": bm.Account,
    "Balance": bm.Balance,
    "Transaction": bm.Transaction,
    "Card": bm.Card,
    "CardControls": bm.CardControls,
    "CardAuthorisation": bm.CardAuthorisation,
    "FraudFlag": bm.FraudFlag,
    "ScheduledPayment": bm.ScheduledPayment,
    "PaymentAttempt": bm.PaymentAttempt,
    "Payee": bm.Payee,
    "Mandate": bm.Mandate,
    "DirectDebitCollection": bm.DirectDebitCollection,
}


def _is_optional(annotation: Any) -> bool:
    return (
        type(None) in typing.get_args(annotation)
        if isinstance(annotation, UnionType) or typing.get_origin(annotation) is typing.Union
        else False
    )


def _literal_values(annotation: Any) -> set[str] | None:
    for arg in (annotation, *typing.get_args(annotation)):
        if typing.get_origin(arg) is Literal:
            return set(typing.get_args(arg))
    return None


@pytest.mark.parametrize(("name", "model"), BANKING_MODELS.items())
def test_banking_models_match_contract(name: str, model: type[BaseModel]) -> None:
    schema = BANKING["components"]["schemas"][name]
    properties = set(schema["properties"])
    required = set(schema.get("required", []))
    fields = model.model_fields
    assert set(fields) == properties, f"{name}: fields differ from the contract"
    for field_name, info in fields.items():
        if field_name in required:
            assert info.is_required(), f"{name}.{field_name} is required by the contract"
            assert not _is_optional(info.annotation), f"{name}.{field_name} must not be nullable"
        else:
            assert not info.is_required(), f"{name}.{field_name} is optional in the contract"
        prop = schema["properties"][field_name]
        literal = _literal_values(info.annotation)
        if literal is not None and "enum" in prop:
            assert literal == {v for v in prop["enum"] if v is not None}, f"{name}.{field_name} enum differs"


def test_client_covers_every_read_only_endpoint() -> None:
    """Every GET operation in the banking contract has a client method; the client calls
    nothing else (no simulation endpoint)."""
    get_ops = {
        op["operationId"] for item in BANKING["paths"].values() for method, op in item.items() if method == "get"
    }
    mock = ContractBankingMock()

    async def call_everything() -> None:
        client = BankingClient("http://banking.test", transport=mock.transport)
        await client.list_customers()
        await client.get_customer(ALICE)
        await client.list_accounts(ALICE)
        await client.get_balance(ALICE, ALICE_ACCOUNT)
        await client.list_transactions(ALICE, ALICE_ACCOUNT, limit=5)
        await client.list_cards(ALICE)
        await client.get_card_controls(ALICE, ALICE_CARD)
        await client.list_authorisations(ALICE, card_id=ALICE_CARD, result=bm.AuthorisationResult.DECLINED, limit=5)
        await client.list_fraud_flags(ALICE, status=bm.FraudFlagStatus.OPEN)
        await client.list_scheduled_payments(ALICE)
        await client.list_payment_attempts(ALICE, result=[bm.PaymentAttemptResult.FAILED])
        await client.list_payees(ALICE)
        await client.get_payee(ALICE, ALICE_PAYEE)
        await client.list_mandates(ALICE, status=bm.MandateStatus.ACTIVE)
        await client.list_direct_debit_collections(ALICE, result=bm.CollectionResult.FAILED)
        await client.aclose()

    import asyncio

    asyncio.run(call_everything())
    assert set(mock.operations_called) == get_ops
    public = {n for n, _ in inspect.getmembers(BankingClient, inspect.iscoroutinefunction) if not n.startswith("_")}
    assert not any("simulation" in n or "incident" in n for n in public)
    assert all(r.method == "GET" for r in mock.requests)
