"""The banking API mocked from the contract, plus `POST /simulation/incidents`.

`SimulationBankingMock` extends `ContractBankingMock` (the read-only API over the seeded
world) with the simulation endpoint: the request body is validated against
`IncidentRequest` and the response against the 201 `Incident` schema of
`contracts/banking-api.yaml`. Incidents reuse the seeded customers whose data already
shows the situation (Alice: frozen card, failed standing order, cancelled mandate; Bob:
fraud block), so the agents' tools see consistent facts.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from tests.banking_mock import (
    ALICE,
    ALICE_ACCOUNT,
    ALICE_CARD,
    ALICE_MANDATE_RENT,
    ALICE_SCHEDULED,
    BANKING_URI,
    BOB,
    BOB_ACCOUNT,
    BOB_CARD,
    NOW,
    ContractBankingMock,
    ContractViolation,
    response_validator,
    schema_validator,
)


@dataclass(frozen=True)
class MockScenario:
    type: str
    root_cause: str
    outcome: str
    customer_id: str
    knows: list[str]
    complaint: str
    affected: dict[str, str]
    phrasing: tuple[str, ...] = ()


SCENARIOS: list[MockScenario] = [
    MockScenario(
        "card_decline",
        "card_frozen_by_customer",
        "self_serve",
        ALICE,
        [
            "the card ending 4242 was declined 2 time(s) today at Tesco",
            "there is enough money in the account",
            "they used the banking app last week but do not remember changing anything",
        ],
        "My card was declined at Tesco today even though I have money in my account. What is going on?",
        {"account_id": ALICE_ACCOUNT, "card_id": ALICE_CARD},
        ("my card keeps getting declined",),
    ),
    MockScenario(
        "card_decline",
        "fraud_rule_triggered",
        "needs_human",
        BOB,
        [
            "they tried to pay £899.00 to GadgetWorld Online online today and it was declined",
            "later the card was declined at the supermarket too",
            "they did not get any message from the bank",
        ],
        "My card was declined online and now it does not work anywhere. I need it working today.",
        {"account_id": BOB_ACCOUNT, "card_id": BOB_CARD},
        ("my card doesn't work anywhere",),
    ),
    MockScenario(
        "failed_direct_debit",
        "mandate_cancelled_by_customer",
        "self_serve",
        ALICE,
        [
            "Acme Lettings says this month's payment of £650.00 was not made",
            "the payment is normally taken automatically",
            "they cancelled some old direct debits a few weeks ago",
        ],
        "My letting agent says my rent didn't go out this month. Please look into it.",
        {"account_id": ALICE_ACCOUNT, "mandate_id": ALICE_MANDATE_RENT},
        ("my letting agent says the direct debit failed",),
    ),
    MockScenario(
        "missed_payment",
        "insufficient_funds",
        "self_serve",
        ALICE,
        [
            "Landlord Ltd says this month's rent of £750.00 has not arrived",
            "it is a standing order that has worked for months",
        ],
        "My landlord says my rent did not arrive this month. Please check.",
        {"account_id": ALICE_ACCOUNT, "scheduled_payment_id": ALICE_SCHEDULED},
        ("my standing order didn't pay",),
    ),
]


class SimulationBankingMock(ContractBankingMock):
    def __init__(self) -> None:
        super().__init__()
        self.incidents: list[dict[str, Any]] = []
        self.incident_requests: list[dict[str, Any]] = []
        self.simulation_enabled = True
        self._incident_request = schema_validator(BANKING_URI, "IncidentRequest", registry=self.registry)
        self._incident_response = response_validator("/simulation/incidents", "post", "201", registry=self.registry)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/simulation/incidents" and request.method == "POST":
            return self._create_incident(request)
        return super().handle(request)

    def _create_incident(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.operations_called.append("createIncident")
        if not self.simulation_enabled:
            return self._problem(404, "Not Found")
        body = json.loads(request.content or b"{}")
        errors = [e.message for e in self._incident_request.iter_errors(body)]
        if errors:
            raise ContractViolation(f"createIncident: request does not match the contract: {errors[:3]}")
        self.incident_requests.append(body)
        if body.get("type") == "other":
            return self._problem(400, "type 'other' cannot be simulated")
        candidates = [
            s
            for s in SCENARIOS
            if (body.get("type") in (None, s.type)) and (body.get("root_cause") in (None, s.root_cause))
        ]
        if not candidates:
            return self._problem(400, "No simulation scenario for this type and root cause")
        scenario = candidates[0]
        customer = self.world[scenario.customer_id].customer
        incident = {
            "incident_id": str(uuid.uuid4()),
            "issue_type": scenario.type,
            "customer_id": scenario.customer_id,
            "customer_name": customer["name"],
            "persona_hints": {
                "vagueness": "medium",
                "patience": "medium",
                "tone": "frustrated",
                "phrasing": list(scenario.phrasing),
                "knows": scenario.knows,
                "max_turns": 6,
            },
            "customer_knowledge": scenario.knows,
            "complaint": scenario.complaint,
            "affected": scenario.affected,
            "expected_root_cause": scenario.root_cause,
            "expected_outcome": scenario.outcome,
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
        }
        errors = [e.message for e in self._incident_response.iter_errors(incident)]
        if errors:
            raise ContractViolation(f"createIncident: mock response does not match the contract: {errors[:3]}")
        self.incidents.append(incident)
        return httpx.Response(201, json=incident)
