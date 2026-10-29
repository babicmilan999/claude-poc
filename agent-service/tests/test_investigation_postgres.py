"""The investigation package on a real Postgres: the SQL store, the Postgres checkpointer
and the worker pool woken by NOTIFY. Skipped by default.

    TEST_DATABASE_URL=postgresql://app:app@localhost:5432/app_test uv run pytest -m integration
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.banking import BankingClient
from core.db.session import create_engine as create_async_engine_from_settings
from core.db.session import make_sessionmaker
from core.enums import CaseSource, CaseStatus, InvestigationStatus, IssueType, RootCause, Verdict
from core.jobs import JobQueue
from core.settings import AGENT_SERVICE_DIR, Settings
from investigation.checkpoint import postgres_checkpointer, psycopg_url
from investigation.schemas import FeedbackRequest
from investigation.store import Conflict, NewCase
from investigation.store_sql import SqlInvestigationStore
from investigation.worker import WorkerPool
from tests.banking_mock import AGENT_URI, ALICE, contract_registry, schema_validator
from tests.conftest import integration_database_url
from tests.fakes import FakeLLMProvider, tool_call
from tests.fakes import text as say
from tests.investigation.harness import Harness, diagnosis, enough, make_harness, triaged

pytestmark = pytest.mark.integration

DB_URL = integration_database_url()
if DB_URL is None:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)

REGISTRY = contract_registry()


def _settings() -> Settings:
    return Settings(database_url=DB_URL)


@pytest.fixture(scope="module")
def migrated() -> None:
    engine = create_engine(_settings().sqlalchemy_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS agents CASCADE"))
        conn.execute(text("DROP SCHEMA IF EXISTS kb CASCADE"))
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
        await session.execute(text("TRUNCATE agents.cases, agents.conversations CASCADE"))
    yield maker
    await engine.dispose()


@pytest.fixture
async def harness(sessionmaker: async_sessionmaker[AsyncSession], banking: BankingClient) -> AsyncIterator[Harness]:
    store = SqlInvestigationStore(sessionmaker, JobQueue(sessionmaker))
    async with postgres_checkpointer(DB_URL) as checkpointer:  # type: ignore[arg-type]
        yield make_harness(FakeLLMProvider(), banking, store=store, checkpointer=checkpointer)


def _script(llm: FakeLLMProvider) -> None:
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        say("Frozen (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1"),
        say("The card is frozen (ev-1)."),
    )


def _valid(name: str, body: Any) -> None:
    schema_validator(AGENT_URI, name, registry=REGISTRY).validate(body)


async def _new_case(h: Harness) -> uuid.UUID:
    case = await h.service.create_case(
        NewCase(customer_id=uuid.UUID(ALICE), customer_name="Alice", source=CaseSource.EMAIL, complaint="declined")
    )
    _valid("Case", case.model_dump(mode="json"))
    return case.id


async def test_full_review_cycle_on_postgres(harness: Harness) -> None:
    h = harness
    case_id = await _new_case(h)
    _script(h.llm)
    assert await h.service.run_pending() == 1

    case = await h.store.get_case(case_id)
    assert case is not None and case.status == CaseStatus.AWAITING_REVIEW and case.category == IssueType.CARD_DECLINE
    inv_id = case.active_investigation_id
    assert inv_id is not None
    investigation = await h.store.get_investigation(inv_id)
    assert investigation is not None and investigation.diagnosis is not None
    _valid("Investigation", investigation.model_dump(mode="json"))
    events = await h.store.list_events(inv_id)
    for event in events:
        _valid("InvestigationEvent", event.model_dump(mode="json"))
    assert [e.type for e in events][-2:] == ["awaiting_review", "status_changed"]

    rejected = await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.REJECTED, hint="look again"))
    assert rejected.investigation.status == InvestigationStatus.QUEUED
    _script(h.llm)
    await h.service.run_pending()
    again = await h.store.get_investigation(inv_id)
    assert again is not None and again.status == InvestigationStatus.AWAITING_REVIEW and again.review is None
    assert again.diagnosis is not None and investigation.diagnosis.id != again.diagnosis.id

    accepted = await h.service.submit_feedback(inv_id, FeedbackRequest(verdict=Verdict.ACCEPTED))
    assert accepted.investigation.status == InvestigationStatus.COMPLETED
    await h.service.finalize(inv_id, accepted.resume_payload)
    snapshot = await h.service.graph.aget_state({"configurable": {"thread_id": str(inv_id)}})
    assert snapshot.values["outcome"] == "finalized"
    detail = await h.store.get_case_detail(case_id)
    assert detail is not None and detail.status == CaseStatus.RESOLVED
    _valid("CaseDetail", detail.model_dump(mode="json"))


async def test_reinvestigate_and_one_active_investigation(harness: Harness) -> None:
    h = harness
    case_id = await _new_case(h)
    with pytest.raises(Conflict):
        await h.service.start_investigation(case_id, reinvestigate=True)  # still queued
    _script(h.llm)
    await h.service.run_pending()
    started = await h.service.start_investigation(case_id, reinvestigate=True)
    assert started.created and started.superseded_id is not None
    old = await h.store.get_investigation(started.superseded_id)
    assert old is not None and old.status == InvestigationStatus.SUPERSEDED
    same = await h.service.start_investigation(case_id, reinvestigate=False)
    assert not same.created and same.investigation.id == started.investigation.id


async def test_worker_pool_runs_jobs_woken_by_notify(harness: Harness) -> None:
    h = harness
    pool = WorkerPool(h.service, count=2, database_url=psycopg_url(DB_URL))  # type: ignore[arg-type]
    await pool.start()
    try:
        _script(h.llm)
        case_id = await _new_case(h)
        for _ in range(100):
            case = await h.store.get_case(case_id)
            assert case is not None
            if case.status == CaseStatus.AWAITING_REVIEW:
                break
            await asyncio.sleep(0.1)
        assert case.status == CaseStatus.AWAITING_REVIEW
    finally:
        await pool.stop()


def test_app_lifespan_on_postgres(
    migrated: None, banking_mock: Any, fake_llm: FakeLLMProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real wiring: SQL store, Postgres checkpointer, worker pool and the case events
    listener, started by the app lifespan."""
    import time

    from fastapi.testclient import TestClient

    from core.app import create_app
    from core.context import AppContext
    from core.kb import KnowledgeBaseSearch
    from core.kb.embeddings import HashingEmbedder
    from core.kb.store import InMemoryKnowledgeBaseStore
    from core.tracing import Tracing
    from tests.banking_mock import NOW
    from tests.conftest import BANKING_URL

    monkeypatch.setenv("INVESTIGATION_POLL_SECONDS", "0.2")
    settings = Settings(
        database_url=DB_URL, banking_api_url=BANKING_URL, kb_ingest_on_startup=False, llm_profile="test"
    )

    def factory(s: Settings) -> AppContext:
        engine = create_async_engine_from_settings(s)
        maker = make_sessionmaker(engine)
        return AppContext(
            settings=s,
            banking=BankingClient(s.banking_api_url, transport=banking_mock.transport),
            llm=fake_llm,
            tracing=Tracing(client=None, profile="test"),
            kb=KnowledgeBaseSearch(store=InMemoryKnowledgeBaseStore(), embedder=HashingEmbedder()),
            engine=engine,
            sessionmaker=maker,
            jobs=JobQueue(maker),
            extras={"investigation_clock": lambda: NOW},
        )

    app = create_app(settings, context_factory=factory, feature_modules=("investigation.api",), migrate=lambda s: None)
    _script(fake_llm)
    with TestClient(app) as client:
        case = client.post(
            "/cases", json={"customer_id": ALICE, "source": "email", "complaint": "card declined"}
        ).json()
        for _ in range(100):
            if client.get(f"/cases/{case['id']}").json()["status"] == "awaiting_review":
                break
            time.sleep(0.1)
        detail = client.get(f"/cases/{case['id']}").json()
        _valid("CaseDetail", detail)
        assert detail["status"] == "awaiting_review"
        inv_id = detail["investigations"][0]["id"]
        stream = client.get(f"/investigations/{inv_id}/events", headers={"Accept": "text/event-stream"})
        assert "event: awaiting_review" in stream.text
        response = client.post(f"/investigations/{inv_id}/feedback", json={"verdict": "accepted"})
        assert response.status_code == 202 and response.json()["status"] == "completed"
        assert client.get(f"/cases/{case['id']}").json()["status"] == "resolved"
