"""Simulate customer and Simulate email through the FastAPI app, with the assistant,
investigation and simulation packages plugged in by `core.app`; bodies validated against
`contracts/agent-api.yaml`, the banking simulation mocked from `banking-api.yaml`."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.banking import BankingClient
from core.context import AppContext
from core.enums import CaseSource
from core.kb import KnowledgeBaseSearch
from core.settings import Settings
from core.tracing import Tracing
from investigation.service import InvestigationService, trace_id_for
from investigation.store_memory import MemoryInvestigationStore
from simulation.api import TRANSPORT_KEY
from simulation.service import SimulationService
from tests.banking_mock import AGENT_URI, ALICE, NOW, contract_registry, schema_validator
from tests.conftest import BANKING_URL, FIXTURE_KB
from tests.evals.fake_models import perfect_models
from tests.evals.helpers import FakeLangfuse
from tests.fakes import FakeLLMProvider
from tests.simulation.banking_sim_mock import SimulationBankingMock

REGISTRY = contract_registry()
FEATURES = ("assistant.api", "investigation.api", "simulation.api")
SECRETS = ("expected", "card_frozen_by_customer", "mandate_cancelled_by_customer", "self_serve", "needs_human")


def _valid(name: str, body: Any) -> Any:
    schema_validator(AGENT_URI, name, registry=REGISTRY).validate(body)
    return body


def _problem(response: Any, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    _valid("Problem", response.json())


def _no_secrets(body: Any) -> None:
    text = json.dumps(body)
    for secret in SECRETS:
        assert secret not in text, secret


@pytest.fixture
def mock() -> SimulationBankingMock:
    return SimulationBankingMock()


@pytest.fixture
def llm() -> FakeLLMProvider:
    return perfect_models()


def _client(
    enabled: bool,
    mock: SimulationBankingMock,
    llm: FakeLLMProvider,
    kb: KnowledgeBaseSearch,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("INVESTIGATION_WORKERS_ENABLED", "false")
    monkeypatch.setenv("SIMULATION_SCORE_POLL_SECONDS", "0.02")
    settings = Settings(
        banking_api_url=BANKING_URL, kb_path=FIXTURE_KB, kb_ingest_on_startup=False, llm_profile="test",
        simulation_enabled=enabled,
    )  # fmt: skip

    def factory(s: Settings) -> AppContext:
        ctx = AppContext(
            settings=s,
            banking=BankingClient(s.banking_api_url, transport=mock.transport),
            llm=llm,
            tracing=Tracing(client=None, profile=s.llm_profile),
            kb=kb,
        )
        ctx.extras[TRANSPORT_KEY] = mock.transport
        ctx.extras["investigation_clock"] = lambda: NOW
        return ctx

    return TestClient(create_app(settings, context_factory=factory, feature_modules=FEATURES))


@pytest.fixture
def client(
    mock: SimulationBankingMock, llm: FakeLLMProvider, kb: KnowledgeBaseSearch, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _client(True, mock, llm, kb, monkeypatch) as c:
        yield c


def _extras(client: TestClient) -> dict[str, Any]:
    return client.app.state.context.extras  # type: ignore[attr-defined,no-any-return]


def _simulation(client: TestClient) -> SimulationService:
    return _extras(client)["simulation"]  # type: ignore[no-any-return]


def _investigation(client: TestClient) -> InvestigationService:
    return _extras(client)["investigation"]  # type: ignore[no-any-return]


def _call(client: TestClient, fn: Any, *args: Any) -> Any:
    assert client.portal is not None
    return client.portal.call(fn, *args)


def test_disabled_simulation_answers_404(
    mock: SimulationBankingMock, llm: FakeLLMProvider, kb: KnowledgeBaseSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(False, mock, llm, kb, monkeypatch) as client:
        _problem(client.post("/simulation/customers", json={"type": "card_decline"}), 404)
        _problem(client.post("/simulation/emails"), 404)
        assert "simulation" not in _extras(client)
    assert mock.incident_requests == []


def test_simulate_customer_runs_a_hidden_journey(
    client: TestClient, mock: SimulationBankingMock, llm: FakeLLMProvider
) -> None:
    response = client.post("/simulation/customers", json={"type": "card_decline"})
    assert response.status_code == 202, response.text
    body = _valid("SimulatedCustomer", response.json())
    assert set(body) == {"simulation_id", "conversation_id", "customer_id", "customer_name"}
    assert body["customer_id"] == ALICE and body["customer_name"] == "Alice Example"
    _no_secrets(body)
    assert mock.incident_requests == [{"type": "card_decline"}]

    _call(client, _simulation(client).wait)

    conversation = _valid("ConversationDetail", client.get(f"/conversations/{body['conversation_id']}").json())
    assert conversation["simulated"] is True and conversation["status"] == "closed"
    assert conversation["outcome"] == "resolved" and conversation["category"] == "card_decline"
    assert [m["role"] for m in conversation["messages"]] == ["customer", "assistant"]
    _no_secrets(conversation)
    # The simulated customer chatted through the assistant: guard, triage, answer, output guard.
    assert len(llm.fake("simulator").calls) == 2 and llm.fake("triage").calls and llm.fake("judge").calls
    listed = client.get("/conversations", params={"simulated": "true"}).json()
    assert [c["id"] for c in listed["items"]] == [body["conversation_id"]]


def test_simulate_email_creates_a_simulated_case_that_is_investigated(client: TestClient) -> None:
    response = client.post("/simulation/emails", json={"type": "failed_direct_debit"})
    assert response.status_code == 201, response.text
    case = _valid("Case", response.json())
    assert case["source"] == CaseSource.SIMULATED_EMAIL.value and case["category"] is None
    assert case["simulated"] is True and case["status"] == "new" and case["active_investigation_id"]
    assert case["complaint"].startswith("My letting agent says")
    _no_secrets(case)

    # Stored with its answer key for scoring (never returned).
    store = _investigation(client).store
    assert isinstance(store, MemoryInvestigationStore)
    row = store.cases[uuid.UUID(case["id"])]
    assert (row.expected_root_cause, row.expected_outcome) == ("mandate_cancelled_by_customer", "self_serve")

    assert _call(client, _investigation(client).run_pending) == 1
    detail = _valid("CaseDetail", client.get(f"/cases/{case['id']}").json())
    assert detail["status"] == "awaiting_review" and detail["category"] == "failed_direct_debit"
    investigation = client.get(f"/investigations/{detail['investigations'][0]['id']}").json()
    assert investigation["diagnosis"]["root_cause"] == "mandate_cancelled_by_customer"  # found, not told
    assert "expected" not in json.dumps(detail)


def test_simulation_scores_in_langfuse(client: TestClient) -> None:
    langfuse = FakeLangfuse()
    simulation = _simulation(client)
    simulation.score_client = langfuse

    customer = client.post("/simulation/customers", json={"type": "card_decline"}).json()
    case = client.post("/simulation/emails", json={"type": "failed_direct_debit"}).json()
    _call(client, _investigation(client).run_pending)
    _call(client, simulation.wait)

    by_name = {s["name"]: s for s in langfuse.scores}
    assert by_name["handoff_correct"]["value"] == 1.0
    assert by_name["handoff_correct"]["session_id"] == customer["conversation_id"]
    investigation_id = client.get(f"/cases/{case['id']}").json()["investigations"][0]["id"]
    assert by_name["diagnosis_correct"]["value"] == 1.0
    assert by_name["diagnosis_correct"]["trace_id"] == trace_id_for(investigation_id)


@pytest.mark.parametrize("path", ["/simulation/customers", "/simulation/emails"])
def test_bad_requests_and_banking_failures(client: TestClient, mock: SimulationBankingMock, path: str) -> None:
    _problem(client.post(path, json={"type": "other"}), 400)
    _problem(client.post(path, json={"type": "nonsense"}), 400)
    assert mock.incident_requests == []
    mock.simulation_enabled = False  # banking runs without its simulation profile
    _problem(client.post(path, json={"type": "card_decline"}), 502)


def test_random_type_without_a_body(client: TestClient, mock: SimulationBankingMock) -> None:
    assert client.post("/simulation/emails").status_code == 201
    assert mock.incident_requests == [{}]
