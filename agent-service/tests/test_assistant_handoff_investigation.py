"""The assistant's handoff case reaches the investigation package: without a database
through its in-memory store (the app wiring), and with Postgres through the
`NOTIFY case_events` payload its listener reads (the cross-package contract)."""

import functools
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Needs both packages; skipped on a branch that has only the assistant.
pytest.importorskip("investigation.service")

from assistant.service import AssistantService
from assistant.store import HandoffCaseCopy, HandoffCaseRef, case_created_payload
from core.app import create_app
from core.banking import BankingClient
from core.context import AppContext
from core.enums import CaseSource, ExpectedOutcome, IssueType, RootCause
from core.jobs import CASE_EVENTS_CHANNEL
from core.settings import Settings
from investigation.service import InvestigationService, case_event_from_notification
from investigation.store_memory import MemoryInvestigationStore
from investigation.worker import CaseEventsListener
from tests.assistant.helpers import Harness, classified, input_ok, parse_sse, validator
from tests.banking_mock import BOB, NOW
from tests.fakes import FakeLLMProvider, text
from tests.investigation.harness import make_harness

BOB_H = {"X-Customer-Id": BOB}
SUMMARY = "Card blocked for a security check; customer asks why."


def _script_bob_handoff(llm: FakeLLMProvider) -> None:
    """Bob's card is blocked for a security check: `needs_human_action` hands off."""
    llm.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    llm.script("assistant", text(SUMMARY))


# --- without a database: the app hands the case to the investigation store ---------------


@pytest.fixture
def client(settings: Settings, app_context_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("INVESTIGATION_WORKERS_ENABLED", "false")

    def factory(s: Settings) -> AppContext:
        ctx: AppContext = app_context_factory(s)
        ctx.extras["investigation_clock"] = lambda: NOW
        return ctx

    app = create_app_with_both(settings, factory)
    with TestClient(app) as test_client:
        yield test_client


def create_app_with_both(settings: Settings, factory: Any) -> Any:
    return create_app(settings, context_factory=factory, feature_modules=("assistant.api", "investigation.api"))


def _extras(client: TestClient) -> dict[str, Any]:
    return client.app.state.context.extras  # type: ignore[attr-defined,no-any-return]


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    assert client.portal is not None
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def test_without_a_database_the_handoff_case_is_queued_for_investigation(
    client: TestClient, fake_llm: FakeLLMProvider
) -> None:
    assistant: AssistantService = _extras(client)["assistant"]
    investigation: InvestigationService = _extras(client)["investigation"]
    store = investigation.store
    assert isinstance(store, MemoryInvestigationStore)
    case_events = investigation.broker.subscribe_cases()

    conversation = _call(
        client,
        assistant.create_conversation,
        BOB,
        simulated=True,
        expected_outcome=ExpectedOutcome.NEEDS_HUMAN,
        expected_root_cause=RootCause.FRAUD_RULE_TRIGGERED,
    )
    _script_bob_handoff(fake_llm)
    response = client.post(
        f"/conversations/{conversation.id}/messages", json={"content": "My card keeps getting declined"}, headers=BOB_H
    )
    assert response.status_code == 200, response.text
    events = [data for _, data in parse_sse(response.text)]
    handoff = next(e for e in events if e["type"] == "handoff")
    case_id = handoff["case_id"]

    # The case queue lists it, and its live stream announced it.
    page = client.get("/cases").json()
    assert [c["id"] for c in page["items"]] == [case_id]
    case = page["items"][0]
    validator("Case").validate(case)
    assert case["source"] == "assistant_handoff" and case["category"] == "card_decline"
    assert case["summary"] == SUMMARY and case["simulated"] is True
    assert case["status"] == "new" and case["active_investigation_id"] is not None
    event = case_events.get_nowait()
    assert event["type"] == "case_created" and event["case"]["id"] == case_id

    # The conversation points at that case; the detail carries evidence and transcript.
    assert client.get(f"/conversations/{conversation.id}").json()["case_id"] == case_id
    detail_response = client.get(f"/cases/{case_id}")
    detail = detail_response.json()
    validator("CaseDetail").validate(detail)
    assert detail["handoff_evidence"] and detail["handoff_evidence"][0]["tool"] == "signal_snapshot"
    assert [m["role"] for m in detail["transcript"]] == ["customer", "assistant"]
    # "Open conversation" in the console leads back to the real conversation.
    assert detail["conversation_id"] == str(conversation.id)

    # The answer key is stored for scoring, and shown nowhere.
    row = store.cases[uuid.UUID(case_id)]
    assert row.expected_root_cause == "fraud_rule_triggered" and row.expected_outcome == "needs_human"
    for body in (detail_response.text, response.text, json.dumps(page)):
        assert "expected" not in body and "fraud_rule_triggered" not in body

    # A worker claims it and its intake has what the assistant found.
    job = _call(client, store.claim_next, "test-worker")
    assert job is not None and str(job.case_id) == case_id
    intake = _call(client, store.load_intake, job.case_id)
    assert intake.source is CaseSource.ASSISTANT_HANDOFF and intake.category is IssueType.CARD_DECLINE
    assert intake.summary == SUMMARY and intake.handoff_evidence == detail["handoff_evidence"]
    assert not hasattr(intake, "expected_root_cause")


async def test_without_the_investigation_package_the_case_stays_in_the_assistant_store(
    fake_llm: FakeLLMProvider, banking: BankingClient
) -> None:
    async def no_store(_: HandoffCaseCopy) -> HandoffCaseRef | None:
        return None

    h = Harness(fake_llm, banking, None)
    h.store.handoff_sink = no_store
    conversation = await h.service.create_conversation(BOB)
    _script_bob_handoff(fake_llm)
    events = await h.service.run_turn(conversation, "My card keeps getting declined")
    case_id = h.store.cases[0]["id"]
    assert next(e for e in events if e["type"] == "handoff")["case_id"] == str(case_id)
    assert (CASE_EVENTS_CHANNEL, case_created_payload(case_id)) in h.store.notifications


# --- with Postgres: the NOTIFY payload the investigation listener reads ------------------


@dataclass
class _Notify:
    payload: str


class _FakeConnection:
    """Stands in for the psycopg connection `CaseEventsListener` reads `notifies()` from."""

    def __init__(self, payloads: list[str]) -> None:
        self.payloads = payloads

    async def notifies(self) -> AsyncIterator[_Notify]:
        for payload in self.payloads:
            yield _Notify(payload)


async def test_the_assistants_case_created_notify_reaches_the_investigation_intake(
    fake_llm: FakeLLMProvider, banking: BankingClient
) -> None:
    investigation = make_harness(fake_llm, banking)
    shared = investigation.store
    assert isinstance(shared, MemoryInvestigationStore)

    async def shared_table(case: HandoffCaseCopy) -> HandoffCaseRef:
        """What the SQL store writes to the shared `cases` table (no announcement: in
        Postgres the NOTIFY below is the only signal the investigation process gets)."""
        case_id = shared.add_case(
            customer_id=case.customer_id,
            source=CaseSource.ASSISTANT_HANDOFF,
            complaint=case.complaint,
            category=case.category,
            summary=case.summary,
            handoff_evidence=case.evidence,
            transcript=case.transcript,
            customer_name=case.customer_name,
        )
        return HandoffCaseRef(case_id, None)

    assistant = Harness(fake_llm, banking, None)
    assistant.store.handoff_sink = shared_table
    conversation = await assistant.service.create_conversation(BOB)
    _script_bob_handoff(fake_llm)
    events = await assistant.service.run_turn(conversation, "My card keeps getting declined")
    case_id = next(e for e in events if e["type"] == "handoff")["case_id"]

    payload = case_created_payload(uuid.UUID(case_id))  # exactly what SqlConversationStore NOTIFYs
    assert case_event_from_notification(payload) == (None, None, case_id)

    case_events = investigation.service.broker.subscribe_cases()
    listener = CaseEventsListener(investigation.service, "postgresql://unused", CASE_EVENTS_CHANNEL)
    await listener._read(_FakeConnection([payload]))

    event = case_events.get_nowait()
    assert event["type"] == "case_created" and event["case"]["id"] == case_id
    validator("CaseCreatedEvent").validate(event)

    job = await shared.claim_next("test-worker")
    assert job is not None and str(job.case_id) == case_id
    intake = await shared.load_intake(job.case_id)
    assert intake.source is CaseSource.ASSISTANT_HANDOFF and intake.category is IssueType.CARD_DECLINE
    assert intake.summary == SUMMARY
    assert intake.handoff_evidence and intake.handoff_evidence[0]["tool"] == "signal_snapshot"
    assert intake.transcript[0] == ("customer", "My card keeps getting declined")
