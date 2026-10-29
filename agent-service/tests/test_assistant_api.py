"""Conversation endpoints through FastAPI, checked against `contracts/agent-api.yaml`:
bodies, problem responses, the SSE chat stream and watch mode."""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from assistant.store import InMemoryConversationStore
from core.app import create_app
from core.banking import BankingClient
from core.context import AppContext
from core.enums import ExpectedOutcome, IssueType, RootCause
from core.kb import KnowledgeBaseSearch
from core.settings import Settings
from core.tracing import Tracing
from tests.assistant.helpers import CARD_ANSWER, classified, input_ok, judge, parse_sse, validate_events, validator
from tests.banking_mock import ALICE, BOB, UNKNOWN_CUSTOMER, ContractBankingMock
from tests.fakes import FakeLLMProvider, text, tool_call

ALICE_H = {"X-Customer-Id": ALICE}
BOB_H = {"X-Customer-Id": BOB}


@pytest.fixture
def store() -> InMemoryConversationStore:
    return InMemoryConversationStore()


@pytest.fixture
def client(
    settings: Settings,
    banking_mock: ContractBankingMock,
    fake_llm: FakeLLMProvider,
    kb: KnowledgeBaseSearch,
    store: InMemoryConversationStore,
) -> Iterator[TestClient]:
    def factory(s: Settings) -> AppContext:
        ctx = AppContext(
            settings=s,
            banking=BankingClient(s.banking_api_url, transport=banking_mock.transport),
            llm=fake_llm,
            tracing=Tracing(client=None, profile=s.llm_profile),
            kb=kb,
        )
        ctx.extras["assistant_store"] = store
        return ctx

    app = create_app(settings, context_factory=factory, feature_modules=("assistant.api",))
    with TestClient(app) as c:
        yield c


def _problem(response: Any, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    validator("Problem").validate(response.json())


def _start(client: TestClient, headers: dict[str, str] = ALICE_H) -> dict[str, Any]:
    response = client.post("/conversations", headers=headers)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    validator("Conversation").validate(body)
    return body


def _send(client: TestClient, conversation_id: str, content: str, headers: dict[str, str] = ALICE_H) -> list[dict]:
    response = client.post(f"/conversations/{conversation_id}/messages", json={"content": content}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = parse_sse(response.text)
    for name, data in messages:
        assert name == data["type"]  # the SSE event name is the event type
        assert data["conversation_id"] == conversation_id
    events = [data for _, data in messages]
    validate_events(events)
    return events


def _script_answer(llm: FakeLLMProvider) -> None:
    llm.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    llm.script(
        "assistant",
        tool_call("get_my_cards"),
        tool_call("search_knowledge_base", {"query": "frozen card"}),
        text(CARD_ANSWER),
    )
    llm.script("judge", judge(True))


def test_create_conversation(client: TestClient) -> None:
    body = _start(client)
    assert body["customer_id"] == ALICE and body["customer_name"] == "Alice Example"
    assert body["status"] == "open" and body["simulated"] is False and body["outcome"] is None


@pytest.mark.parametrize(
    "headers", [{}, {"X-Customer-Id": "nope"}, {"X-Customer-Id": UNKNOWN_CUSTOMER}], ids=["missing", "bad", "unknown"]
)
def test_customer_endpoints_need_a_known_customer(client: TestClient, headers: dict[str, str]) -> None:
    _problem(client.post("/conversations", headers=headers), 401)
    conversation = _start(client)
    _problem(client.post(f"/conversations/{conversation['id']}/messages", json={"content": "hi"}, headers=headers), 401)
    _problem(
        client.put(f"/conversations/{conversation['id']}/messages/1/rating", json={"rating": "up"}, headers=headers),
        401,
    )


def test_a_resolved_conversation_end_to_end(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    conversation = _start(client)
    _script_answer(fake_llm)

    events = _send(client, conversation["id"], "Why was my card declined at Tesco?")

    kinds = [e["type"] for e in events]
    assert kinds[0] == "message_created" and kinds[-2:] == ["sources", "message_completed"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == CARD_ANSWER
    answer = events[-1]["message"]

    rated = client.put(
        f"/conversations/{conversation['id']}/messages/{answer['id']}/rating", json={"rating": "up"}, headers=ALICE_H
    )
    assert rated.status_code == 200
    validator("Message").validate(rated.json())
    assert rated.json()["rating"] == "up"

    detail = client.get(f"/conversations/{conversation['id']}")
    assert detail.status_code == 200
    body = detail.json()
    validator("ConversationDetail").validate(body)
    assert body["outcome"] == "resolved" and body["rating"] == "up" and body["status"] == "open"
    assert body["category"] == "card_decline" and body["case_id"] is None
    assert [m["role"] for m in body["messages"]] == ["customer", "assistant"]
    assert body["messages"][1]["sources"] == answer["sources"]


def test_handoff_over_the_api(client: TestClient, fake_llm: FakeLLMProvider, store: InMemoryConversationStore) -> None:
    conversation = _start(client, BOB_H)
    fake_llm.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
    fake_llm.script("assistant", text("Card blocked for a security check; customer asks why."))

    events = _send(client, conversation["id"], "My card keeps getting declined", BOB_H)

    assert [e["type"] for e in events] == ["message_created", "message_created", "handoff", "conversation_status"]
    detail = client.get(f"/conversations/{conversation['id']}").json()
    validator("ConversationDetail").validate(detail)
    assert detail["status"] == "closed" and detail["outcome"] == "handed_off"
    assert detail["case_id"] == events[2]["case_id"] == str(store.cases[0]["id"])
    assert detail["ended_at"] is not None

    # Closed conversations take no more messages.
    response = client.post(f"/conversations/{conversation['id']}/messages", json={"content": "hello?"}, headers=BOB_H)
    _problem(response, 409)

    # Watch mode on a closed conversation: replay, then the stream ends.
    watched = client.get(f"/conversations/{conversation['id']}/events")
    assert watched.status_code == 200 and watched.headers["content-type"].startswith("text/event-stream")
    replay = [data for _, data in parse_sse(watched.text)]
    validate_events(replay)
    assert [e["type"] for e in replay] == ["message_created", "message_created", "conversation_status"]
    assert replay[-1]["status"] == "closed" and replay[-1]["outcome"] == "handed_off"


def test_someone_elses_conversation_does_not_exist(client: TestClient) -> None:
    conversation = _start(client)
    _problem(client.post(f"/conversations/{conversation['id']}/messages", json={"content": "hi"}, headers=BOB_H), 404)
    _problem(
        client.put(f"/conversations/{conversation['id']}/messages/1/rating", json={"rating": "up"}, headers=BOB_H), 404
    )


def test_unknown_conversations_are_404(client: TestClient) -> None:
    missing = "00000000-0000-4000-8000-000000000000"
    _problem(client.get(f"/conversations/{missing}"), 404)
    _problem(client.get("/conversations/not-a-uuid"), 404)
    _problem(client.get(f"/conversations/{missing}/events"), 404)
    _problem(client.post(f"/conversations/{missing}/messages", json={"content": "hi"}, headers=ALICE_H), 404)


def test_message_validation(client: TestClient) -> None:
    conversation = _start(client)
    url = f"/conversations/{conversation['id']}/messages"
    _problem(client.post(url, json={"content": ""}, headers=ALICE_H), 400)
    _problem(client.post(url, json={"content": "   "}, headers=ALICE_H), 400)
    _problem(client.post(url, json={"content": "x" * 4001}, headers=ALICE_H), 400)
    _problem(client.post(url, json={}, headers=ALICE_H), 400)


def test_rating_rules(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    conversation = _start(client)
    events = _send(client, conversation["id"], "hello")  # small talk, no model
    customer, reply = events[0]["message"], events[1]["message"]
    base = f"/conversations/{conversation['id']}/messages"
    _problem(client.put(f"{base}/{customer['id']}/rating", json={"rating": "up"}, headers=ALICE_H), 409)
    _problem(client.put(f"{base}/999/rating", json={"rating": "up"}, headers=ALICE_H), 404)
    _problem(client.put(f"{base}/{reply['id']}/rating", json={"rating": "meh"}, headers=ALICE_H), 400)

    assert client.put(f"{base}/{reply['id']}/rating", json={"rating": "up"}, headers=ALICE_H).status_code == 200
    assert client.get(f"/conversations/{conversation['id']}").json()["outcome"] == "resolved"
    # 👎 after 👍 takes the resolution back.
    down = client.put(f"{base}/{reply['id']}/rating", json={"rating": "down"}, headers=ALICE_H)
    assert down.status_code == 200 and down.json()["rating"] == "down"
    body = client.get(f"/conversations/{conversation['id']}").json()
    assert body["outcome"] is None and body["rating"] == "down"


def test_list_conversations(client: TestClient, store: InMemoryConversationStore) -> None:
    alice = _start(client)
    _start(client, BOB_H)
    simulated = asyncio.run(
        store.create_conversation(
            uuid.UUID(BOB),
            "Bob Example",
            simulated=True,
            expected_outcome=ExpectedOutcome.NEEDS_HUMAN,
            expected_root_cause=RootCause.FRAUD_RULE_TRIGGERED,
        )
    )
    page = validator("ConversationPage")

    everything = client.get("/conversations")
    assert everything.status_code == 200
    page.validate(everything.json())
    assert everything.json()["total"] == 3
    assert "expected" not in everything.text and "needs_human" not in everything.text
    assert "fraud_rule_triggered" not in everything.text

    watchable = client.get("/conversations", params={"simulated": "true", "status": "open"}).json()
    assert [c["id"] for c in watchable["items"]] == [str(simulated.id)]
    mine = client.get("/conversations", params={"customer_id": ALICE}).json()
    assert [c["id"] for c in mine["items"]] == [alice["id"]]
    paged = client.get("/conversations", params={"limit": 1, "offset": 1}).json()
    assert len(paged["items"]) == 1 and paged["total"] == 3
    detail = client.get(f"/conversations/{simulated.id}")
    assert "expected" not in detail.text and "fraud_rule_triggered" not in detail.text

    _problem(client.get("/conversations", params={"limit": 0}), 400)
    _problem(client.get("/conversations", params={"status": "pending"}), 400)


def test_a_failed_turn_streams_an_error(client: TestClient, fake_llm: FakeLLMProvider) -> None:
    conversation = _start(client)
    fake_llm.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))  # nothing for `assistant`
    events = _send(client, conversation["id"], "Why was my card declined at Tesco?")
    assert [e["type"] for e in events] == ["message_created", "error"]
    # The conversation stays usable.
    _script_answer(fake_llm)
    assert _send(client, conversation["id"], "Why was my card declined at Tesco?")[-1]["type"] == "message_completed"


def test_optional_fields_are_left_out_rather_than_null() -> None:
    """`customer_name` and the `Source` links are optional but not nullable in the contract."""
    from datetime import UTC, datetime

    from assistant.schemas import ConversationOut, SourceOut
    from core.enums import ConversationStatus

    body = ConversationOut(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        status=ConversationStatus.OPEN,
        simulated=False,
        started_at=datetime.now(UTC),
    ).model_dump(mode="json")
    assert "customer_name" not in body
    validator("Conversation").validate(body)
    source = SourceOut(kind="account_data", title="Your cards", tool="get_my_cards").model_dump(mode="json")
    assert source == {"kind": "account_data", "title": "Your cards", "tool": "get_my_cards"}
    validator("Source").validate(source)


async def test_checkpointer_without_a_database_is_in_memory_and_shared(
    settings: Settings, banking: BankingClient
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from assistant.checkpoint import libpq_url, open_checkpointer

    ctx = AppContext(
        settings=settings,
        banking=banking,
        llm=FakeLLMProvider(),
        tracing=Tracing(client=None, profile="test"),
        kb=KnowledgeBaseSearch(store=None, embedder=None),  # type: ignore[arg-type]
    )
    async with open_checkpointer(ctx) as saver:
        assert isinstance(saver, InMemorySaver) and ctx.extras["checkpointer"] is saver
        async with open_checkpointer(ctx) as again:
            assert again is saver
    assert libpq_url("postgresql+psycopg://a@h/db") == "postgresql://a@h/db"
    assert libpq_url("postgres://a@h/db") == "postgresql://a@h/db"
    assert libpq_url("postgresql://a@h/db") == "postgresql://a@h/db"
