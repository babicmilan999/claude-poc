"""The assistant on a real Postgres: the SQL conversation store, the handoff transaction
(case + queued investigation + NOTIFY) and the Postgres checkpointer. Skipped by default.

    TEST_DATABASE_URL=postgresql://app:app@localhost:5432/app_test uv run pytest -m integration
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.checkpoint import CHECKPOINT_SCHEMA, open_checkpointer
from assistant.graph import AssistantDeps, build_assistant_graph
from assistant.service import AssistantService
from assistant.store import NewHandoffCase, SqlConversationStore
from core.banking import BankingClient
from core.context import AppContext
from core.db.models import Case, Investigation
from core.db.session import create_engine as create_async_engine_from_settings
from core.db.session import make_sessionmaker
from core.enums import (
    CaseSource,
    ConversationOutcome,
    ConversationStatus,
    ExpectedOutcome,
    InvestigationStatus,
    IssueType,
    MessageRole,
    Rating,
    RootCause,
)
from core.jobs import CASE_EVENTS_CHANNEL, JobQueue
from core.kb import KnowledgeBaseSearch
from core.kb.embeddings import HashingEmbedder
from core.kb.store import InMemoryKnowledgeBaseStore
from core.settings import AGENT_SERVICE_DIR, Settings
from core.tracing import Tracing
from tests.assistant.helpers import CARD_ANSWER, classified, input_ok, judge
from tests.banking_mock import ALICE, NOW, ContractBankingMock
from tests.conftest import integration_database_url
from tests.fakes import FakeLLMProvider, text, tool_call

pytestmark = pytest.mark.integration

DB_URL = integration_database_url()
if DB_URL is None:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)


def _settings() -> Settings:
    assert DB_URL is not None
    return Settings(database_url=DB_URL, banking_api_url="http://banking.test")


@pytest.fixture(scope="module")
def migrated() -> None:
    engine = create_engine(_settings().sqlalchemy_url)
    with engine.begin() as conn:
        conn.execute(sql("DROP SCHEMA IF EXISTS agents CASCADE"))
        conn.execute(sql("DROP SCHEMA IF EXISTS kb CASCADE"))
        conn.execute(sql(f"DROP SCHEMA IF EXISTS {CHECKPOINT_SCHEMA} CASCADE"))
    engine.dispose()
    config = Config(str(AGENT_SERVICE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(AGENT_SERVICE_DIR / "migrations"))
    config.cmd_opts = type("Opts", (), {"x": [f"url={DB_URL}"]})()  # type: ignore[assignment]
    command.upgrade(config, "head")


@pytest.fixture
async def sessionmaker(migrated: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine_from_settings(_settings())
    maker = make_sessionmaker(engine)
    async with maker.begin() as session:
        await session.execute(sql("TRUNCATE agents.cases, agents.conversations CASCADE"))
    yield maker
    await engine.dispose()


@pytest.fixture
def store(sessionmaker: async_sessionmaker[AsyncSession]) -> SqlConversationStore:
    return SqlConversationStore(sessionmaker, JobQueue(sessionmaker))


async def test_conversations_messages_and_ratings(store: SqlConversationStore) -> None:
    alice = uuid.UUID(ALICE)
    conversation = await store.create_conversation(alice, "Alice Example")
    assert conversation.status is ConversationStatus.OPEN and conversation.case_id is None
    other = await store.create_conversation(uuid.uuid4(), None, simulated=True)

    customer = await store.add_message(conversation.id, MessageRole.CUSTOMER, "why declined?")
    answer = await store.add_message(
        conversation.id, MessageRole.ASSISTANT, "frozen", [{"kind": "account_data", "title": "Your cards"}]
    )
    assert answer.id > customer.id and answer.sources == [{"kind": "account_data", "title": "Your cards"}]
    assert [m.id for m in await store.list_messages(conversation.id)] == [customer.id, answer.id]

    rated = await store.set_message_rating(conversation.id, answer.id, Rating.DOWN)
    assert rated.rating is Rating.DOWN
    assert await store.count_message_ratings(conversation.id, Rating.DOWN) == 1
    assert await store.get_message(conversation.id, answer.id) == rated
    assert await store.get_message(other.id, answer.id) is None

    updated = await store.update_conversation(
        conversation.id, category=IssueType.CARD_DECLINE, rating=Rating.DOWN, outcome=ConversationOutcome.RESOLVED
    )
    assert updated.category is IssueType.CARD_DECLINE and updated.outcome is ConversationOutcome.RESOLVED
    cleared = await store.update_conversation(conversation.id, outcome=None)
    assert cleared.outcome is None and cleared.category is IssueType.CARD_DECLINE

    items, total = await store.list_conversations()
    assert total == 2 and {c.id for c in items} == {conversation.id, other.id}
    items, total = await store.list_conversations(simulated=True, status=ConversationStatus.OPEN)
    assert total == 1 and items[0].id == other.id
    items, total = await store.list_conversations(customer_id=alice, limit=1, offset=0)
    assert [c.id for c in items] == [conversation.id]


async def test_handoff_is_one_transaction_with_a_queued_investigation_and_notify(
    store: SqlConversationStore, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    conversation = await store.create_conversation(
        uuid.UUID(ALICE),
        "Alice Example",
        simulated=True,
        expected_outcome=ExpectedOutcome.SELF_SERVE,
        expected_root_cause=RootCause.CARD_FROZEN_BY_CUSTOMER,
    )
    await store.add_message(conversation.id, MessageRole.CUSTOMER, "talk to a human")

    assert DB_URL is not None
    listener = await psycopg.AsyncConnection.connect(DB_URL, autocommit=True)
    await listener.execute(f'LISTEN "{CASE_EVENTS_CHANNEL}"')
    try:
        result = await store.create_handoff_case(
            NewHandoffCase(
                conversation_id=conversation.id,
                customer_id=conversation.customer_id,
                customer_name=None,
                category=IssueType.CARD_DECLINE,
                complaint="talk to a human",
                summary="Customer asked for a person.",
                evidence=[{"id": "a-ev-1", "origin": "assistant", "tool": "get_my_cards", "summary": "x"}],
                notice="An agent will look into this and get back to you.",
            )
        )
        gen = listener.notifies(timeout=5)
        note = await anext(gen)
        assert json.loads(note.payload) == {"type": "case_created", "case_id": str(result.case_id)}
    finally:
        await listener.close()

    assert result.conversation.status is ConversationStatus.CLOSED
    assert result.conversation.outcome is ConversationOutcome.HANDED_OFF
    assert result.conversation.case_id == result.case_id and result.conversation.ended_at is not None
    assert result.notice.role is MessageRole.ASSISTANT

    async with sessionmaker() as session:
        case = (await session.execute(select(Case).where(Case.id == result.case_id))).scalar_one()
        job = (await session.execute(select(Investigation).where(Investigation.case_id == case.id))).scalar_one()
    assert case.source == CaseSource.ASSISTANT_HANDOFF.value and case.status == "new"
    assert case.customer_name == "Alice Example" and case.category == "card_decline"
    assert case.simulated is True
    assert case.expected_root_cause == "card_frozen_by_customer" and case.expected_outcome == "self_serve"
    assert job.status == InvestigationStatus.QUEUED.value and job.id == result.investigation_id

    fetched = await store.get_conversation(conversation.id)
    assert fetched is not None and fetched.case_id == result.case_id


async def test_the_graph_on_postgres(
    store: SqlConversationStore, sessionmaker, banking_mock: ContractBankingMock
) -> None:
    settings = _settings()
    banking = BankingClient(settings.banking_api_url, transport=banking_mock.transport)
    llm = FakeLLMProvider()
    ctx = AppContext(
        settings=settings,
        banking=banking,
        llm=llm,
        tracing=Tracing(client=None, profile="test"),
        kb=KnowledgeBaseSearch(InMemoryKnowledgeBaseStore(), HashingEmbedder()),
        sessionmaker=sessionmaker,
    )
    deps = AssistantDeps(llm=llm, banking=banking, kb=ctx.kb, store=store, triage_threshold=0.6, clock=lambda: NOW)
    async with open_checkpointer(ctx) as checkpointer:
        assert ctx.extras["checkpointer"] is checkpointer
        service = AssistantService(
            graph=build_assistant_graph(deps, checkpointer), store=store, banking=banking, tracing=ctx.tracing
        )
        conversation = await service.create_conversation(ALICE)
        llm.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
        llm.script("assistant", tool_call("get_my_cards"), text(CARD_ANSWER))
        llm.script("judge", judge(True))
        events = await service.run_turn(conversation, "Why was my card declined at Tesco?")
        assert events[-1]["type"] == "message_completed"

        # A fresh graph on the same checkpointer sees the thread.
        again = build_assistant_graph(deps, checkpointer)
        state = await again.aget_state({"configurable": {"thread_id": str(conversation.id)}})
        assert [e["tool"] for e in state.values["evidence"]] == ["get_my_cards"]
        assert len(state.values["messages"]) == 2

        llm.script("triage", input_ok(), classified(IssueType.CARD_DECLINE))
        llm.script("assistant", text("Summary"))
        events = await service.run_turn(conversation, "Can I talk to someone?")
        assert [e["type"] for e in events][-2:] == ["handoff", "conversation_status"]
    assert "checkpointer" not in ctx.extras

    async with sessionmaker() as session:
        tables = (
            await session.execute(
                sql("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
                {"s": CHECKPOINT_SCHEMA},
            )
        ).scalars()
        assert "checkpoints" in set(tables)
    await banking.aclose()
    await asyncio.sleep(0)
