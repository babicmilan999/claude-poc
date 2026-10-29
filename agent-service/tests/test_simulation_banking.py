"""The banking simulation client against `POST /simulation/incidents` mocked from the
contract (request and response validated), and the customer / answer-key split."""

import httpx
import pytest

from core.enums import ExpectedOutcome, IssueType, RootCause
from simulation.banking import (
    IncidentRequest,
    SimulationBankingClient,
    SimulationError,
    SimulationRejected,
    SimulationUnavailable,
)
from tests.banking_mock import ALICE, BOB
from tests.conftest import BANKING_URL
from tests.simulation.banking_sim_mock import SimulationBankingMock


@pytest.fixture
def mock() -> SimulationBankingMock:
    return SimulationBankingMock()


@pytest.fixture
async def client(mock: SimulationBankingMock):  # type: ignore[no-untyped-def]
    async with SimulationBankingClient(BANKING_URL, transport=mock.transport) as c:
        yield c


async def test_create_incident_sends_a_contract_request(
    client: SimulationBankingClient, mock: SimulationBankingMock
) -> None:
    incident = await client.create_incident(
        IncidentRequest(
            type=IssueType.CARD_DECLINE, root_cause=RootCause.FRAUD_RULE_TRIGGERED, params={"merchant_name": "X"}
        )
    )
    assert mock.incident_requests == [
        {"type": "card_decline", "root_cause": "fraud_rule_triggered", "params": {"merchant_name": "X"}}
    ]
    assert str(incident.customer_id) == BOB and incident.customer_name == "Bob Example"
    assert incident.answer_key().outcome is ExpectedOutcome.NEEDS_HUMAN
    assert incident.answer_key().root_cause is RootCause.FRAUD_RULE_TRIGGERED
    assert incident.answer_key().issue_type is IssueType.CARD_DECLINE


async def test_empty_request_is_a_random_incident(client: SimulationBankingClient, mock: SimulationBankingMock) -> None:
    incident = await client.create_incident()
    assert mock.incident_requests == [{}]
    assert str(incident.customer_id) == ALICE


async def test_customer_view_has_the_persona_but_no_answer_key(client: SimulationBankingClient) -> None:
    incident = await client.create_incident(IncidentRequest(type=IssueType.CARD_DECLINE))
    view = incident.customer_view()
    assert view.persona.knows == incident.customer_knowledge
    assert view.persona.tone == "frustrated" and view.persona.max_turns == 6
    dumped = view.model_dump_json()
    for secret in ("expected", incident.expected_root_cause.value, incident.expected_outcome.value):
        assert secret not in dumped
    assert not any(f.startswith("expected") for f in type(view).model_fields)


async def test_errors(mock: SimulationBankingMock, client: SimulationBankingClient) -> None:
    with pytest.raises(SimulationRejected, match="other"):
        await client.create_incident(IncidentRequest(type=IssueType.OTHER))
    with pytest.raises(SimulationRejected):
        await client.create_incident(IncidentRequest(type=IssueType.CARD_DECLINE, root_cause=RootCause.CARD_EXPIRED))
    mock.simulation_enabled = False
    with pytest.raises(SimulationUnavailable):
        await client.create_incident()


async def test_unreachable_and_unexpected_responses() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with SimulationBankingClient(BANKING_URL, transport=httpx.MockTransport(down)) as client:
        with pytest.raises(SimulationError, match="unreachable"):
            await client.create_incident()

    async with SimulationBankingClient(
        BANKING_URL, transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"nope": 1}))
    ) as client:
        with pytest.raises(SimulationError, match="invalid incident"):
            await client.create_incident()

    async with SimulationBankingClient(
        BANKING_URL, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as client:
        with pytest.raises(SimulationError, match="500"):
            await client.create_incident()
