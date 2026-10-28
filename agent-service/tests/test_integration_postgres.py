"""Postgres-only tests: migrations, the job queue (SKIP LOCKED, NOTIFY) and pgvector
hybrid search. Skipped by default.

Run against a scratch database with pgvector (it is wiped):
    TEST_DATABASE_URL=postgresql://app:app@localhost:5432/app_test uv run pytest -m integration
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Base, Case, Investigation
from core.db.session import create_engine as create_async_engine_from_settings
from core.db.session import make_sessionmaker
from core.enums import CaseSource, InvestigationStatus
from core.jobs import JobQueue, NotificationListener
from core.kb import KnowledgeBaseSearch
from core.kb.embeddings import HashingEmbedder
from core.kb.ingest import ingest_knowledge_base
from core.kb.store import PostgresKnowledgeBaseStore
from core.settings import AGENT_SERVICE_DIR, Settings
from tests.conftest import FIXTURE_KB, integration_database_url

pytestmark = pytest.mark.integration

DB_URL = integration_database_url()
if DB_URL is None:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)


def _settings() -> Settings:
    return Settings(database_url=DB_URL)


@pytest.fixture(scope="module")
def migrated() -> None:
    sync_url = _settings().sqlalchemy_url
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS agents CASCADE"))
        conn.execute(text("DROP SCHEMA IF EXISTS kb CASCADE"))
    engine.dispose()
    config = Config(str(AGENT_SERVICE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(AGENT_SERVICE_DIR / "migrations"))
    config.cmd_opts = type("Opts", (), {"x": [f"url={DB_URL}"]})()  # type: ignore[assignment]
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture
async def sessionmaker(migrated: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine_from_settings(_settings())
    maker = make_sessionmaker(engine)
    async with maker.begin() as session:
        await session.execute(text("TRUNCATE agents.cases, agents.conversations, kb.kb_chunks CASCADE"))
    yield maker
    await engine.dispose()


def test_migrated_schema_matches_the_models(migrated: None) -> None:
    engine = create_engine(_settings().sqlalchemy_url)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "include_schemas": True,
                "include_name": lambda name, type_, _: (
                    (name in {"agents", "kb"}) if type_ == "schema" else name != "alembic_version"
                ),
                "compare_type": True,
            },
        )
        assert compare_metadata(context, Base.metadata) == []
    engine.dispose()


async def _new_case(session: AsyncSession) -> Case:
    case = Case(id=uuid.uuid4(), customer_id=uuid.uuid4(), source=CaseSource.EMAIL.value, complaint="declined")
    session.add(case)
    await session.flush()
    return case


def test_startup_migration_is_idempotent(migrated: None) -> None:
    """What the app runs at startup (MIGRATE_ON_STARTUP) on an up-to-date database."""
    from core.db.migrate import should_migrate, upgrade_head

    settings = _settings()
    assert should_migrate(settings)
    upgrade_head(settings)


async def test_enqueue_and_claim_with_skip_locked(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    queue = JobQueue(sessionmaker)
    async with sessionmaker.begin() as session:
        cases = [await _new_case(session) for _ in range(5)]
        for case in cases:
            await queue.enqueue(session, case.id)

    claims = await asyncio.gather(*(queue.claim_next(f"w{i}") for i in range(8)))
    claimed = [c for c in claims if c is not None]
    assert len(claimed) == 5
    assert len({c.investigation_id for c in claimed}) == 5, "a job was claimed twice"
    assert all(c.attempts == 1 for c in claimed)
    assert await queue.claim_next("late") is None

    async with sessionmaker() as session:
        statuses = (await session.execute(select(Investigation.status))).scalars().all()
    assert set(statuses) == {InvestigationStatus.RUNNING.value}


async def test_one_active_investigation_per_case(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    queue = JobQueue(sessionmaker)
    async with sessionmaker.begin() as session:
        case = await _new_case(session)
        await queue.enqueue(session, case.id)
    with pytest.raises(IntegrityError):
        async with sessionmaker.begin() as session:
            await queue.enqueue(session, case.id)


async def test_notify_on_commit_wakes_the_listener(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    queue = JobQueue(sessionmaker)
    listener = NotificationListener(DB_URL, [queue.channel])  # type: ignore[arg-type]
    await listener.start()
    try:
        assert await listener.wait(0.2) is False
        async with sessionmaker.begin() as session:
            case = await _new_case(session)
            await queue.enqueue(session, case.id)
        assert await listener.wait(5) is True
    finally:
        await listener.close()


async def test_retry_then_fail(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    queue = JobQueue(sessionmaker)
    async with sessionmaker.begin() as session:
        await queue.enqueue(session, (await _new_case(session)).id)
    job = await queue.claim_next("w1")
    assert job is not None
    assert await queue.release_for_retry(job, "boom", max_attempts=2) is InvestigationStatus.QUEUED
    job = await queue.claim_next("w1")
    assert job is not None and job.attempts == 2
    assert await queue.release_for_retry(job, "boom again", max_attempts=2) is InvestigationStatus.FAILED
    async with sessionmaker() as session:
        row = (await session.execute(select(Investigation))).scalar_one()
    assert row.status == "failed" and row.error == "boom again" and row.finished_at is not None


async def test_stale_jobs_are_requeued(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    queue = JobQueue(sessionmaker)
    async with sessionmaker.begin() as session:
        await queue.enqueue(session, (await _new_case(session)).id)
    job = await queue.claim_next("dead-worker")
    assert job is not None
    assert await queue.heartbeat(job.investigation_id, "dead-worker") is True
    assert await queue.heartbeat(job.investigation_id, "someone-else") is False
    async with sessionmaker.begin() as session:
        await session.execute(update(Investigation).values(heartbeat_at=text("now() - interval '10 minutes'")))
    requeued, failed = await queue.requeue_stale(stale_after=timedelta(minutes=5), max_attempts=3)
    assert requeued == [job.investigation_id] and failed == []
    again = await queue.claim_next("w2")
    assert again is not None and again.attempts == 2

    # The dead worker comes back: it no longer owns the job and cannot change it.
    assert await queue.release_for_retry(job, "late failure", max_attempts=3) is None
    assert await queue.finish(job, InvestigationStatus.COMPLETED) is False
    async with sessionmaker() as session:
        row = (await session.execute(select(Investigation))).scalar_one()
    assert row.status == "running" and row.worker_id == "w2" and row.error is None
    assert await queue.finish(again, InvestigationStatus.COMPLETED) is True
    async with sessionmaker() as session:
        row = (await session.execute(select(Investigation))).scalar_one()
    assert row.status == "completed" and row.worker_id is None and row.finished_at is not None


async def test_requeue_with_resume_payload(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    queue = JobQueue(sessionmaker)
    async with sessionmaker.begin() as session:
        await queue.enqueue(session, (await _new_case(session)).id)
    job = await queue.claim_next("w1")
    assert job is not None
    async with sessionmaker.begin() as session:
        await queue.requeue(session, job.investigation_id, {"verdict": "rejected", "hint": "check the mandate"})
    resumed = await queue.claim_next("w1")
    assert resumed is not None and resumed.resume_payload == {"verdict": "rejected", "hint": "check the mandate"}


async def test_postgres_hybrid_search(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    store = PostgresKnowledgeBaseStore(sessionmaker)
    embedder = HashingEmbedder()
    report = await ingest_knowledge_base(FIXTURE_KB, store, embedder, base_url="/help/")
    assert len(report.added) == 3
    again = await ingest_knowledge_base(FIXTURE_KB, store, embedder, base_url="/help/")
    assert again.chunks == 0
    kb = KnowledgeBaseSearch(store=store, embedder=embedder)
    hits = await kb.search("how do I unfreeze my frozen card", k=3)
    assert hits[0].article_id == "why-cards-get-declined"
    hits = await kb.search("direct debit mandate cancelled", k=2)
    assert hits[0].article_id == "direct-debits"
    # No keyword match at all: the vector side still returns the nearest chunks.
    assert len(await kb.search("zzzz qqqq", k=2)) == 2
