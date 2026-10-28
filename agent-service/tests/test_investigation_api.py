"""Ops endpoints for cases and investigations through the FastAPI app (the package
plugged in by `core.app`), with response bodies and SSE events validated against
`contracts/agent-api.yaml`."""

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.context import AppContext
from core.enums import CaseSource, IssueType, RootCause
from core.settings import Settings
from investigation.api import case_event_stream
from investigation.service import InvestigationService
from investigation.store_memory import MemoryInvestigationStore
from tests.banking_mock import AGENT_URI, ALICE, NOW, UNKNOWN_CUSTOMER, contract_registry, schema_validator
from tests.fakes import FakeLLMProvider, text, tool_call
from tests.investigation.harness import diagnosis, enough, triaged

REGISTRY = contract_registry()


def _validator(name: str) -> Any:
    return schema_validator(AGENT_URI, name, registry=REGISTRY)


def _valid(name: str, body: Any) -> Any:
    _validator(name).validate(body)
    return body


@pytest.fixture
def client(settings: Settings, app_context_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("INVESTIGATION_WORKERS_ENABLED", "false")

    def factory(s: Settings) -> AppContext:
        ctx: AppContext = app_context_factory(s)
        ctx.extras["investigation_clock"] = lambda: NOW
        return ctx

    app = create_app(settings, context_factory=factory, feature_modules=("investigation.api",))
    with TestClient(app) as test_client:
        yield test_client


def _service(client: TestClient) -> InvestigationService:
    return client.app.state.context.extras["investigation"]  # type: ignore[attr-defined,no-any-return]


def _store(client: TestClient) -> MemoryInvestigationStore:
    store = _service(client).store
    assert isinstance(store, MemoryInvestigationStore)
    return store


def _run_jobs(client: TestClient) -> int:
    assert client.portal is not None
    return client.portal.call(_service(client).run_pending)


def _script_card_decline(llm: FakeLLMProvider) -> None:
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("Card frozen (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1"),
        text("The card ending 4242 is frozen by the customer (ev-1)."),
    )


def _create_and_diagnose(client: TestClient, llm: FakeLLMProvider) -> tuple[str, str]:
    response = client.post("/cases", json={"customer_id": ALICE, "source": "email", "complaint": "card declined"})
    assert response.status_code == 201
    case = response.json()
    _script_card_decline(llm)
    assert _run_jobs(client) == 1
    return case["id"], case["active_investigation_id"]


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            fields[key] = value[1:] if value.startswith(" ") else value
        if "data" in fields:
            events.append(fields)
    return events


# --- cases ---------------------------------------------------------------------------------


def test_create_case_queues_an_auto_investigation(client: TestClient) -> None:
    response = client.post(
        "/cases", json={"customer_id": ALICE, "source": "phone", "complaint": "my card was declined twice"}
    )
    assert response.status_code == 201
    case = _valid("Case", response.json())
    assert case["status"] == "new" and case["source"] == "phone" and case["category"] is None
    assert case["customer_name"] == "Alice Example"
    investigation = client.get(f"/investigations/{case['active_investigation_id']}").json()
    assert _valid("Investigation", investigation)["status"] == "queued"
    assert investigation["trigger"] == "auto"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"customer_id": UNKNOWN_CUSTOMER, "source": "email", "complaint": "x"}, 404),
        ({"customer_id": ALICE, "source": "email"}, 400),
        ({"customer_id": ALICE, "source": "assistant_handoff", "complaint": "x"}, 400),
        ({"customer_id": "not-a-uuid", "source": "email", "complaint": "x"}, 400),
        ({"customer_id": ALICE, "source": "email", "complaint": ""}, 400),
    ],
    ids=["unknown-customer", "no-complaint-or-payload", "source-not-allowed", "bad-uuid", "empty-complaint"],
)
def test_create_case_errors(client: TestClient, body: dict[str, Any], status: int) -> None:
    response = client.post("/cases", json=body)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    _valid("Problem", response.json())


def test_create_case_from_a_trigger_payload(client: TestClient) -> None:
    response = client.post(
        "/cases", json={"customer_id": ALICE, "source": "ops_manual", "trigger_payload": {"event": "dd_failed"}}
    )
    assert response.status_code == 201
    assert _valid("Case", response.json())["trigger_payload"] == {"event": "dd_failed"}


def test_case_queue_lists_open_cases_oldest_first(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    first_case, inv_id = _create_and_diagnose(client, fake_llm)
    second = client.post("/cases", json={"customer_id": ALICE, "source": "email", "complaint": "another"}).json()

    page = _valid("CasePage", client.get("/cases").json())
    assert [c["id"] for c in page["items"]] == [first_case, second["id"]] and page["total"] == 2
    assert page["items"][0]["status"] == "awaiting_review" and page["items"][0]["category"] == "card_decline"

    only_new = client.get("/cases", params={"status": "new"}).json()
    assert [c["id"] for c in only_new["items"]] == [second["id"]]
    assert client.get("/cases", params={"category": "card_decline"}).json()["total"] == 1
    assert client.get("/cases", params={"source": "phone"}).json()["total"] == 0
    assert client.get("/cases", params={"limit": 1, "offset": 1}).json()["items"][0]["id"] == second["id"]
    assert client.get("/cases", params={"status": "new,bogus"}).status_code == 400
    assert client.get("/cases", params={"limit": 0}).status_code == 400

    client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "accepted"})
    assert [c["id"] for c in client.get("/cases").json()["items"]] == [second["id"]], "resolved hidden by default"
    resolved = client.get("/cases", params={"status": "resolved,new"}).json()
    assert resolved["total"] == 2


def test_case_detail_with_handoff_transcript_and_investigations(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    store = _store(client)
    case_id = store.add_case(
        customer_id=ALICE,
        customer_name="Alice Example",
        source=CaseSource.ASSISTANT_HANDOFF,
        category=IssueType.CARD_DECLINE,
        summary="Card declined at Tesco.",
        handoff_evidence=[
            {
                "id": "ev-1",
                "origin": "assistant",
                "tool": "get_my_recent_declines",
                "summary": "2 declines: card frozen",
                "created_at": "2026-10-28T10:00:00Z",
            }
        ],
        transcript=[("customer", "my card keeps getting declined"), ("assistant", "I'll pass this to the team.")],
        simulated=True,
        expected_root_cause="card_frozen_by_customer",
        expected_outcome="self_serve",
    )
    fake_llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("Frozen (ev-2)."),
        enough("ev-2"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1", "ev-2"),
        text("Frozen."),
    )
    _run_jobs(client)

    response = client.get(f"/cases/{case_id}")
    detail = _valid("CaseDetail", response.json())
    assert [m["role"] for m in detail["transcript"]] == ["customer", "assistant"]
    assert detail["handoff_evidence"][0]["tool"] == "get_my_recent_declines"
    assert len(detail["investigations"]) == 1 and detail["investigations"][0]["status"] == "awaiting_review"
    assert detail["simulated"] is True and detail["summary"] == "Card declined at Tesco."
    investigation = _valid("Investigation", client.get(f"/investigations/{detail['investigations'][0]['id']}").json())
    assert investigation["category_changed"] is False
    for body in (response.text, json.dumps(investigation), json.dumps(client.get("/cases").json())):
        assert "expected_" not in body and "self_serve" not in body
    assert client.get(f"/cases/{uuid.uuid4()}").status_code == 404


# --- investigations and review ---------------------------------------------------------------


def test_investigation_and_events_match_the_contract(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    _, inv_id = _create_and_diagnose(client, fake_llm)
    investigation = _valid("Investigation", client.get(f"/investigations/{inv_id}").json())
    assert investigation["status"] == "awaiting_review"
    assert investigation["diagnosis"]["root_cause"] == "card_frozen_by_customer"
    assert investigation["evidence"][0]["id"] == "ev-1"

    events = client.get(f"/investigations/{inv_id}/events", headers={"Accept": "application/json"}).json()
    validator = _validator("InvestigationEvent")
    for event in events:
        validator.validate(event)
    ids = [e["id"] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    types = [e["type"] for e in events]
    assert {"step_started", "classified", "tool_call", "evidence", "token", "diagnosis", "awaiting_review"} <= set(
        types
    )
    assert types[-1] == "status_changed"

    assert client.get(f"/investigations/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/investigations/{uuid.uuid4()}/events").status_code == 404


def test_sse_replays_stored_events_and_resumes_after_last_event_id(
    client: TestClient, fake_llm: FakeLLMProvider
) -> None:
    _, inv_id = _create_and_diagnose(client, fake_llm)
    stored = client.get(f"/investigations/{inv_id}/events").json()

    response = client.get(f"/investigations/{inv_id}/events", headers={"Accept": "text/event-stream"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    sse = _parse_sse(response.text)
    validator = _validator("InvestigationEvent")
    for message in sse:
        data = json.loads(message["data"])
        validator.validate(data)
        assert message["event"] == data["type"] and message["id"] == str(data["id"])
    assert [int(m["id"]) for m in sse] == [e["id"] for e in stored], "the stream closes once awaiting review"

    middle = stored[len(stored) // 2]["id"]
    resumed = _parse_sse(
        client.get(
            f"/investigations/{inv_id}/events",
            headers={"Accept": "text/event-stream", "Last-Event-ID": str(middle)},
        ).text
    )
    assert [int(m["id"]) for m in resumed] == [e["id"] for e in stored if e["id"] > middle]
    by_query = _parse_sse(
        client.get(
            f"/investigations/{inv_id}/events", params={"after_id": middle}, headers={"Accept": "text/event-stream"}
        ).text
    )
    assert by_query == resumed
    json_after = client.get(f"/investigations/{inv_id}/events", params={"after_id": middle}).json()
    assert [e["id"] for e in json_after] == [e["id"] for e in stored if e["id"] > middle]
    bad = client.get(f"/investigations/{inv_id}/events", headers={"Last-Event-ID": "abc"})
    assert bad.status_code == 400


async def test_sse_streams_live_events_until_the_run_stops(banking, fake_llm: FakeLLMProvider) -> None:
    """A client that connects while the run is in progress gets the rest live."""
    from investigation.api import investigation_event_stream
    from tests.investigation.harness import make_harness

    h = make_harness(fake_llm, banking, investigation_stream_poll_seconds=5)
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    inv_id = h.only_investigation(case_id).id
    _script_card_decline(fake_llm)

    async def collect() -> list[dict[str, Any]]:
        return [message async for message in investigation_event_stream(h.service, inv_id, None)]

    reader = asyncio.create_task(collect())
    await asyncio.sleep(0.05)
    assert not reader.done(), "queued: the stream stays open"
    await h.service.run_pending()
    messages = await asyncio.wait_for(reader, timeout=3)
    assert [m["event"] for m in messages][-2:] == ["awaiting_review", "status_changed"]
    assert len(messages) == len(h.events(inv_id))


def test_accept_returns_the_new_status_and_finalizes(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    case_id, inv_id = _create_and_diagnose(client, fake_llm)
    response = client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "accepted", "notes": "agreed"})
    assert response.status_code == 202
    body = _valid("Investigation", response.json())
    assert body["status"] == "completed" and body["verdict"] == "accepted" and body["finished_at"]
    assert body["review"]["notes"] == "agreed"
    assert client.get(f"/cases/{case_id}").json()["status"] == "resolved"
    assert client.get(f"/cases/{case_id}").json()["active_investigation_id"] is None
    events = client.get(f"/investigations/{inv_id}/events").json()
    assert {"type": "review_recorded"}.items() <= next(e for e in events if e["type"] == "review_recorded").items()
    assert any(e["type"] == "step_started" and e["payload"]["node"] == "finalize" for e in events), "finalized"
    for event in events:
        _validator("InvestigationEvent").validate(event)

    again = client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "accepted"})
    assert again.status_code == 409


def test_correct_and_feedback_errors(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    _, inv_id = _create_and_diagnose(client, fake_llm)
    assert client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "corrected"}).status_code == 400
    assert client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "rejected"}).status_code == 400
    assert client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "maybe"}).status_code == 400
    assert client.post(f"/investigations/{uuid.uuid4()}/feedback", json={"verdict": "accepted"}).status_code == 404
    response = client.post(
        f"/investigations/{inv_id}/feedback",
        json={"verdict": "corrected", "corrected_root_cause": "card_expired", "corrected_issue_type": "card_decline"},
    )
    assert response.status_code == 202
    body = _valid("Investigation", response.json())
    assert body["verdict"] == "corrected" and body["review"]["corrected_root_cause"] == "card_expired"


def test_reject_with_hint_requeues_the_same_investigation(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    case_id, inv_id = _create_and_diagnose(client, fake_llm)
    response = client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "rejected", "hint": "check fraud"})
    assert response.status_code == 202
    body = _valid("Investigation", response.json())
    assert body["status"] == "queued" and body["verdict"] == "rejected" and body["review"]["hint"] == "check fraud"
    case = client.get(f"/cases/{case_id}").json()
    assert case["status"] == "investigating" and case["active_investigation_id"] == inv_id

    _script_card_decline(fake_llm)
    _run_jobs(client)
    after = _valid("Investigation", client.get(f"/investigations/{inv_id}").json())
    assert after["status"] == "awaiting_review" and after["review"] is None and after["attempts"] == 2
    assert len(client.get(f"/cases/{case_id}").json()["investigations"]) == 1


def test_investigate_and_reinvestigate_endpoints(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    case_id, inv_id = _create_and_diagnose(client, fake_llm)

    existing = client.post(f"/cases/{case_id}/investigate")
    assert existing.status_code == 200 and _valid("Investigation", existing.json())["id"] == inv_id

    re = client.post(f"/cases/{case_id}/reinvestigate")
    assert re.status_code == 201
    new = _valid("Investigation", re.json())
    assert new["id"] != inv_id and new["status"] == "queued" and new["trigger"] == "manual"
    assert client.get(f"/investigations/{inv_id}").json()["status"] == "superseded"
    conflict = client.post(f"/cases/{case_id}/reinvestigate")
    assert conflict.status_code == 409 and conflict.headers["content-type"].startswith("application/problem+json")

    _script_card_decline(fake_llm)
    _run_jobs(client)
    client.post(f"/investigations/{new['id']}/feedback", json={"verdict": "accepted"})
    assert client.post(f"/cases/{case_id}/reinvestigate").status_code == 409
    assert client.post(f"/cases/{case_id}/investigate").status_code == 409
    assert client.post(f"/cases/{uuid.uuid4()}/investigate").status_code == 404
    assert client.post(f"/cases/{uuid.uuid4()}/reinvestigate").status_code == 404

    detail = _valid("CaseDetail", client.get(f"/cases/{case_id}").json())
    assert [i["status"] for i in detail["investigations"]] == ["completed", "superseded"], "newest first"


def test_investigate_a_needs_manual_case(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    case = client.post("/cases", json={"customer_id": ALICE, "source": "email", "complaint": "address change"}).json()
    fake_llm.script("triage", triaged(IssueType.OTHER))
    _run_jobs(client)
    assert client.get(f"/cases/{case['id']}").json()["status"] == "needs_manual"
    parked = client.get(f"/investigations/{case['active_investigation_id']}").json()
    assert _valid("Investigation", parked)["status"] == "completed" and parked["diagnosis"] is None

    response = client.post(f"/cases/{case['id']}/investigate")
    assert response.status_code == 201
    assert _valid("Investigation", response.json())["trigger"] == "manual"
    assert client.get(f"/cases/{case['id']}").json()["status"] == "investigating"


# --- case queue stream -------------------------------------------------------------------------


class _Request:
    async def is_disconnected(self) -> bool:
        return False


async def test_case_queue_stream_carries_case_created_and_status_changed(banking, fake_llm: FakeLLMProvider) -> None:
    from investigation.store import NewCase
    from tests.investigation.harness import make_harness

    h = make_harness(fake_llm, banking)
    stream = case_event_stream(h.service, _Request())  # type: ignore[arg-type]
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    case = await h.service.create_case(
        NewCase(customer_id=uuid.UUID(ALICE), customer_name="Alice Example", source=CaseSource.EMAIL, complaint="x")
    )
    created = await asyncio.wait_for(first, timeout=1)
    validator = _validator("CaseEvent")
    data = json.loads(created["data"])
    validator.validate(data)
    assert created["event"] == "case_created" and data["case"]["id"] == str(case.id)

    _script_card_decline(fake_llm)
    await h.service.run_pending()
    changes = []
    for _ in range(3):  # new → investigating, the category, investigating → awaiting_review
        message = await asyncio.wait_for(anext(stream), timeout=1)
        data = json.loads(message["data"])
        validator.validate(data)
        changes.append((data.get("previous_status"), data["status"], data["category"]))
    assert changes == [
        ("new", "investigating", None),
        ("investigating", "investigating", "card_decline"),
        ("investigating", "awaiting_review", "card_decline"),
    ]
    await stream.aclose()  # type: ignore[attr-defined]
    assert h.service.broker.case_subscribers == 0
