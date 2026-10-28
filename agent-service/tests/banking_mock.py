"""The banking API mocked from `contracts/banking-api.yaml`.

`ContractBankingMock` is an `httpx` transport that:
- routes requests by the contract's paths and methods (anything else is a test failure:
  the client called an endpoint the contract does not have);
- rejects query parameters the contract does not declare for that operation;
- serves a small seeded world (`seed_world()`), applying the contract's filters;
- validates every response body against the contract's response schema (JSON Schema
  2020-12, with the `$ref`s into `scenario.schema.json` resolved).

So the client, tools and snapshot are tested against the contract, not against the
mock's own idea of the API.
"""

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
BANKING_URI = "https://claude-poc.local/contracts/banking-api.yaml"
AGENT_URI = "https://claude-poc.local/contracts/agent-api.yaml"
SCENARIO_URI = "https://claude-poc.local/contracts/scenario.schema.json"

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
UNKNOWN_CUSTOMER = "99999999-9999-4999-8999-999999999999"

ALICE_ACCOUNT = "a1a1a1a1-0000-4000-8000-000000000001"
ALICE_CARD = "c1c1c1c1-0000-4000-8000-000000000001"
ALICE_PAYEE = "f1f1f1f1-0000-4000-8000-000000000001"
ALICE_SCHEDULED = "e1e1e1e1-0000-4000-8000-000000000001"
ALICE_MANDATE_RENT = "d1d1d1d1-0000-4000-8000-000000000001"
ALICE_MANDATE_GYM = "d1d1d1d1-0000-4000-8000-000000000002"
BOB_ACCOUNT = "a2a2a2a2-0000-4000-8000-000000000002"
BOB_CARD = "c2c2c2c2-0000-4000-8000-000000000002"


# --- contract loading ----------------------------------------------------------------


def load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONTRACTS_DIR / name).read_text(encoding="utf-8"))


def load_json(name: str) -> dict[str, Any]:
    import json

    return json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))


def contract_registry() -> Registry:
    resources = [
        (BANKING_URI, load_yaml("banking-api.yaml")),
        (AGENT_URI, load_yaml("agent-api.yaml")),
        (SCENARIO_URI, load_json("scenario.schema.json")),
    ]
    return Registry().with_resources(
        (uri, Resource.from_contents(doc, default_specification=DRAFT202012)) for uri, doc in resources
    )


def _pointer(*parts: str) -> str:
    return "/".join(quote(p.replace("~", "~0").replace("/", "~1"), safe="") for p in parts)


def response_validator(
    path: str, method: str, status: str = "200", registry: Registry | None = None
) -> Draft202012Validator:
    ref = f"{BANKING_URI}#/{_pointer('paths', path, method, 'responses', status, 'content', 'application/json', 'schema')}"
    return Draft202012Validator({"$ref": ref}, registry=registry or contract_registry())


def schema_validator(uri: str, schema_name: str, registry: Registry | None = None) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"{uri}#/components/schemas/{schema_name}"}, registry=registry or contract_registry()
    )


@dataclass
class Operation:
    path: str
    method: str
    operation_id: str
    pattern: re.Pattern[str]
    query_params: set[str]


def contract_operations(spec: dict[str, Any]) -> list[Operation]:
    components = spec.get("components", {}).get("parameters", {})

    def resolve(param: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in param:
            return components[param["$ref"].rsplit("/", 1)[1]]
        return param

    ops: list[Operation] = []
    for path, item in spec["paths"].items():
        shared = [resolve(p) for p in item.get("parameters", [])]
        regex = "^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(path)) + "$"
        for method in ("get", "post", "put", "patch", "delete"):
            if method not in item:
                continue
            op = item[method]
            params = shared + [resolve(p) for p in op.get("parameters", [])]
            ops.append(
                Operation(
                    path=path,
                    method=method,
                    operation_id=op["operationId"],
                    pattern=re.compile(regex),
                    query_params={p["name"] for p in params if p["in"] == "query"},
                )
            )
    return ops


# --- seeded world ----------------------------------------------------------------------


@dataclass
class CustomerWorld:
    customer: dict[str, Any]
    accounts: list[dict[str, Any]] = field(default_factory=list)
    balances: dict[str, dict[str, Any]] = field(default_factory=dict)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    controls: dict[str, dict[str, Any]] = field(default_factory=dict)
    authorisations: list[dict[str, Any]] = field(default_factory=list)
    fraud_flags: list[dict[str, Any]] = field(default_factory=list)
    scheduled_payments: list[dict[str, Any]] = field(default_factory=list)
    payment_attempts: list[dict[str, Any]] = field(default_factory=list)
    payees: list[dict[str, Any]] = field(default_factory=list)
    mandates: list[dict[str, Any]] = field(default_factory=list)
    collections: list[dict[str, Any]] = field(default_factory=list)


def _id(prefix: str, n: int) -> str:
    return f"{prefix}-0000-4000-8000-{n:012d}"


def seed_world() -> dict[str, CustomerWorld]:
    """Alice: card frozen by herself (declines), a failed standing order (insufficient
    funds), a failed Direct Debit (mandate she cancelled). Bob: card blocked by fraud rule
    FR-12."""
    failed_attempt = {
        "id": _id("b1b1b1b1", 1),
        "scheduled_payment_id": ALICE_SCHEDULED,
        "amount": "750.00",
        "result": "failed",
        "failure_reason": "INSUFFICIENT_FUNDS",
        "retry_count": 2,
        "attempted_at": "2026-10-25T06:00:00Z",
    }
    alice = CustomerWorld(
        customer={
            "id": ALICE,
            "name": "Alice Example",
            "email": "alice@example.com",
            "status": "active",
            "vulnerable": False,
            "created_at": "2024-01-10T09:00:00Z",
        },
        accounts=[
            {
                "id": ALICE_ACCOUNT,
                "customer_id": ALICE,
                "name": "Current account",
                "sort_code": "12-34-56",
                "account_number": "12345678",
                "currency": "GBP",
                "balance": "250.00",
                "overdraft_limit": "0.00",
                "status": "active",
            }
        ],
        balances={
            ALICE_ACCOUNT: {
                "account_id": ALICE_ACCOUNT,
                "currency": "GBP",
                "current": "250.00",
                "available": "250.00",
                "overdraft_limit": "0.00",
                "as_of": "2026-10-28T11:59:00Z",
            }
        },
        transactions=[
            {
                "id": _id("71717171", 1),
                "account_id": ALICE_ACCOUNT,
                "amount": "-12.50",
                "currency": "GBP",
                "type": "card_payment",
                "description": "Coffee House",
                "reference_id": None,
                "booked_at": "2026-10-20T08:30:00Z",
            },
            {
                "id": _id("71717171", 2),
                "account_id": ALICE_ACCOUNT,
                "amount": "1000.00",
                "currency": "GBP",
                "type": "transfer_in",
                "description": "Salary",
                "reference_id": None,
                "booked_at": "2026-10-01T07:00:00Z",
            },
        ],
        cards=[
            {
                "id": ALICE_CARD,
                "account_id": ALICE_ACCOUNT,
                "last4": "4242",
                "brand": "visa",
                "status": "frozen",
                "status_reason": "FROZEN_BY_CUSTOMER",
                "expiry": "2028-05-31",
            }
        ],
        controls={
            ALICE_CARD: {
                "card_id": ALICE_CARD,
                "contactless_limit": "100.00",
                "blocked_merchant_categories": ["7995"],
                "online_payments_enabled": True,
            }
        },
        authorisations=[
            {
                "id": _id("a0a0a0a0", 1),
                "card_id": ALICE_CARD,
                "amount": "54.20",
                "currency": "GBP",
                "merchant_name": "Tesco",
                "merchant_mcc": "5411",
                "channel": "chip_and_pin",
                "result": "declined",
                "decline_reason": "CARD_FROZEN",
                "fraud_rule_id": None,
                "created_at": "2026-10-28T09:00:00Z",
            },
            {
                "id": _id("a0a0a0a0", 2),
                "card_id": ALICE_CARD,
                "amount": "23.10",
                "currency": "GBP",
                "merchant_name": "Tesco",
                "merchant_mcc": "5411",
                "channel": "contactless",
                "result": "declined",
                "decline_reason": "CARD_FROZEN",
                "fraud_rule_id": None,
                "created_at": "2026-10-27T18:00:00Z",
            },
            {
                "id": _id("a0a0a0a0", 3),
                "card_id": ALICE_CARD,
                "amount": "12.50",
                "currency": "GBP",
                "merchant_name": "Coffee House",
                "merchant_mcc": "5814",
                "channel": "contactless",
                "result": "approved",
                "decline_reason": None,
                "fraud_rule_id": None,
                "created_at": "2026-10-20T08:30:00Z",
            },
        ],
        scheduled_payments=[
            {
                "id": ALICE_SCHEDULED,
                "account_id": ALICE_ACCOUNT,
                "payee_id": ALICE_PAYEE,
                "payee_name": "Landlord Ltd",
                "reference": "RENT OCT",
                "amount": "750.00",
                "currency": "GBP",
                "frequency": "monthly",
                "next_run": "2026-11-25",
                "status": "active",
                "last_attempt": failed_attempt,
            }
        ],
        payment_attempts=[failed_attempt],
        payees=[
            {
                "id": ALICE_PAYEE,
                "customer_id": ALICE,
                "name": "Landlord Ltd",
                "sort_code": "20-00-00",
                "account_number": "87654321",
                "account_status": "open",
            }
        ],
        mandates=[
            {
                "id": ALICE_MANDATE_RENT,
                "account_id": ALICE_ACCOUNT,
                "originator": "Acme Lettings",
                "reference": "RENT-42",
                "status": "cancelled",
                "cancelled_by": "customer",
                "created_at": "2025-03-01T10:00:00Z",
                "cancelled_at": "2026-10-20T19:00:00Z",
            },
            {
                "id": ALICE_MANDATE_GYM,
                "account_id": ALICE_ACCOUNT,
                "originator": "City Gym",
                "reference": "GYM-7",
                "status": "active",
                "cancelled_by": None,
                "created_at": "2025-06-01T10:00:00Z",
                "cancelled_at": None,
            },
        ],
        collections=[
            {
                "id": _id("dc0dc0dc", 1),
                "mandate_id": ALICE_MANDATE_RENT,
                "originator": "Acme Lettings",
                "amount": "650.00",
                "currency": "GBP",
                "due_date": "2026-10-26",
                "result": "failed",
                "failure_code": "MANDATE_CANCELLED",
                "processed_at": "2026-10-26T05:00:00Z",
            },
            {
                "id": _id("dc0dc0dc", 2),
                "mandate_id": ALICE_MANDATE_GYM,
                "originator": "City Gym",
                "amount": "35.00",
                "currency": "GBP",
                "due_date": "2026-10-15",
                "result": "collected",
                "failure_code": None,
                "processed_at": "2026-10-15T05:00:00Z",
            },
        ],
    )
    bob = CustomerWorld(
        customer={
            "id": BOB,
            "name": "Bob Example",
            "email": "bob@example.com",
            "status": "active",
            "vulnerable": False,
            "created_at": "2023-05-02T09:00:00Z",
        },
        accounts=[
            {
                "id": BOB_ACCOUNT,
                "customer_id": BOB,
                "name": "Current account",
                "sort_code": "11-22-33",
                "account_number": "99887766",
                "currency": "GBP",
                "balance": "40.00",
                "overdraft_limit": "0.00",
                "status": "active",
            }
        ],
        balances={
            BOB_ACCOUNT: {
                "account_id": BOB_ACCOUNT,
                "currency": "GBP",
                "current": "40.00",
                "available": "15.00",
                "overdraft_limit": "0.00",
                "as_of": "2026-10-28T11:59:00Z",
            }
        },
        cards=[
            {
                "id": BOB_CARD,
                "account_id": BOB_ACCOUNT,
                "last4": "1881",
                "brand": "mastercard",
                "status": "blocked",
                "status_reason": "FRAUD_SUSPECTED",
                "expiry": "2027-01-31",
            }
        ],
        controls={
            BOB_CARD: {
                "card_id": BOB_CARD,
                "contactless_limit": "100.00",
                "blocked_merchant_categories": [],
                "online_payments_enabled": True,
            }
        },
        authorisations=[
            {
                "id": _id("a0a0a0a0", 10),
                "card_id": BOB_CARD,
                "amount": "899.00",
                "currency": "GBP",
                "merchant_name": "GadgetWorld Online",
                "merchant_mcc": "5732",
                "channel": "online",
                "result": "declined",
                "decline_reason": "FRAUD_RULE_TRIGGERED",
                "fraud_rule_id": "FR-12",
                "created_at": "2026-10-28T08:00:00Z",
            }
        ],
        fraud_flags=[
            {
                "id": _id("ff0ff0ff", 1),
                "customer_id": BOB,
                "card_id": BOB_CARD,
                "rule_id": "FR-12",
                "description": "High-value online purchase from a new device",
                "risk_score": 0.93,
                "status": "open",
                "action": "card_blocked",
                "created_at": "2026-10-28T08:00:01Z",
                "cleared_at": None,
            }
        ],
    )
    return {ALICE: alice, BOB: bob}


# --- the mock ------------------------------------------------------------------------------


class ContractViolation(AssertionError):
    pass


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ContractBankingMock:
    def __init__(self, world: dict[str, CustomerWorld] | None = None) -> None:
        self.world = world if world is not None else seed_world()
        self.spec = load_yaml("banking-api.yaml")
        self.registry = contract_registry()
        self.operations = contract_operations(self.spec)
        self.requests: list[httpx.Request] = []
        self.operations_called: list[str] = []
        self.fail_with: dict[str, int] = {}  # operationId -> HTTP status to return instead
        self._validators: dict[tuple[str, str], Draft202012Validator] = {}
        self._handlers: dict[str, Callable[..., Any]] = {
            "listCustomers": self._list_customers,
            "getCustomer": lambda c, q: c.customer,
            "listAccounts": lambda c, q: c.accounts,
            "getAccountBalance": self._balance,
            "listTransactions": self._transactions,
            "listCards": lambda c, q: c.cards,
            "getCardControls": self._controls,
            "listCardAuthorisations": self._authorisations,
            "listFraudFlags": self._fraud_flags,
            "listScheduledPayments": lambda c, q: c.scheduled_payments,
            "listPaymentAttempts": self._payment_attempts,
            "listPayees": lambda c, q: c.payees,
            "getPayee": self._payee,
            "listMandates": self._mandates,
            "listDirectDebitCollections": self._collections,
        }

    # --- httpx transport ---------------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def customer_paths_requested(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method.lower()
        matches = [(op, op.pattern.match(path)) for op in self.operations]
        matches = [(op, m) for op, m in matches if m]
        if not matches:
            raise ContractViolation(f"{request.method} {path}: no such path in banking-api.yaml")
        found = [(op, m) for op, m in matches if op.method == method]
        if not found:
            raise ContractViolation(f"{request.method} {path}: method not in banking-api.yaml")
        op, match = found[0]
        unknown = set(request.url.params.keys()) - op.query_params
        if unknown:
            raise ContractViolation(f"{op.operation_id}: query parameters not in the contract: {sorted(unknown)}")
        self.operations_called.append(op.operation_id)

        if op.operation_id in self.fail_with:
            status = self.fail_with[op.operation_id]
            return self._problem(status, "Injected failure")
        handler = self._handlers.get(op.operation_id)
        if handler is None:
            raise ContractViolation(f"{op.operation_id}: not supported by the mock (agents must not call it)")

        params = match.groupdict()
        query = dict(request.url.params)
        if op.operation_id == "listCustomers":
            body = handler(None, query)
        else:
            customer = self.world.get(params.pop("customer_id"))
            if customer is None:
                return self._problem(404, "Not Found")
            body = handler(customer, query, **params) if params else handler(customer, query)
            if body is None:
                return self._problem(404, "Not Found")
        body = copy.deepcopy(body)
        self._validate(op, body)
        return httpx.Response(200, json=body)

    def _validate(self, op: Operation, body: Any) -> None:
        key = (op.path, op.method)
        if key not in self._validators:
            self._validators[key] = response_validator(op.path, op.method, registry=self.registry)
        errors = sorted(self._validators[key].iter_errors(body), key=lambda e: list(e.path))
        if errors:
            raise ContractViolation(
                f"{op.operation_id}: mock response does not match the contract: "
                + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
            )

    def _problem(self, status: int, title: str) -> httpx.Response:
        return httpx.Response(
            status, json={"title": title, "status": status}, headers={"content-type": "application/problem+json"}
        )

    # --- filters ---------------------------------------------------------------------------

    @staticmethod
    def _window(items: list[dict[str, Any]], query: dict[str, str], ts_field: str) -> list[dict[str, Any]]:
        out = sorted(items, key=lambda i: i[ts_field] or "", reverse=True)
        if "since" in query:
            since = _ts(query["since"])
            out = [i for i in out if i[ts_field] and _ts(i[ts_field]) >= since]
        if "until" in query:
            until = _ts(query["until"])
            out = [i for i in out if i[ts_field] and _ts(i[ts_field]) < until]
        limit = int(query.get("limit", 50))
        if not 1 <= limit <= 200:
            raise ContractViolation(f"limit out of range: {limit}")
        return out[:limit]

    def _list_customers(self, _: Any, query: dict[str, str]) -> list[dict[str, Any]]:
        return [w.customer for w in self.world.values()]

    def _balance(self, c: CustomerWorld, query: dict[str, str], account_id: str) -> Any:
        return c.balances.get(account_id)

    def _transactions(self, c: CustomerWorld, query: dict[str, str], account_id: str) -> Any:
        if account_id not in c.balances:
            return None
        return self._window([t for t in c.transactions if t["account_id"] == account_id], query, "booked_at")

    def _controls(self, c: CustomerWorld, query: dict[str, str], card_id: str) -> Any:
        return c.controls.get(card_id)

    def _authorisations(self, c: CustomerWorld, query: dict[str, str]) -> Any:
        items = c.authorisations
        if "card_id" in query:
            items = [a for a in items if a["card_id"] == query["card_id"]]
        if "result" in query:
            items = [a for a in items if a["result"] == query["result"]]
        return self._window(items, query, "created_at")

    def _fraud_flags(self, c: CustomerWorld, query: dict[str, str]) -> Any:
        items = c.fraud_flags
        if "card_id" in query:
            items = [f for f in items if f["card_id"] == query["card_id"]]
        if "status" in query:
            items = [f for f in items if f["status"] == query["status"]]
        return sorted(items, key=lambda f: f["created_at"], reverse=True)

    def _payment_attempts(self, c: CustomerWorld, query: dict[str, str]) -> Any:
        items = c.payment_attempts
        if "scheduled_payment_id" in query:
            items = [a for a in items if a["scheduled_payment_id"] == query["scheduled_payment_id"]]
        if "result" in query:
            wanted = set(query["result"].split(","))
            items = [a for a in items if a["result"] in wanted]
        return self._window(items, query, "attempted_at")

    def _payee(self, c: CustomerWorld, query: dict[str, str], payee_id: str) -> Any:
        return next((p for p in c.payees if p["id"] == payee_id), None)

    def _mandates(self, c: CustomerWorld, query: dict[str, str]) -> Any:
        items = c.mandates
        if "status" in query:
            items = [m for m in items if m["status"] == query["status"]]
        return items

    def _collections(self, c: CustomerWorld, query: dict[str, str]) -> Any:
        items = c.collections
        if "mandate_id" in query:
            items = [i for i in items if i["mandate_id"] == query["mandate_id"]]
        if "result" in query:
            items = [i for i in items if i["result"] == query["result"]]
        return self._window(items, query, "due_date")
