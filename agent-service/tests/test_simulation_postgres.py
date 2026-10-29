"""The Simulate email case on a real Postgres: one transaction writes the case with its
hidden answer key and a queued investigation, and the SQL investigation store reads it
back without the key. Skipped by default.

    TEST_DATABASE_URL=postgresql://app:app@localhost:5432/app_test uv run pytest -m integration
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.banking import BankingClient
from core.db.models import Case as CaseRow
from core.db.models import Investigation as InvestigationRow
from core.db.session import create_engine as create_async_engine_from_settings
from core.db.session import make_sessionmaker
from core.enums import ExpectedOutcome, IssueType, RootCause
from core.jobs import JobQueue
from core.settings import AGENT_SERVICE_DIR, Settings
from investigation.store_sql import SqlInvestigationStore
from simulation.banking import AnswerKey
from simulation.cases import create_simulated_email_case
from simulation.persona import CustomerView, PersonaSpec
from tests.banking_mock import AGENT_URI, ALICE, contract_registry, schema_validator
from tests.conftest import integration_database_url
from tests.fakes import FakeLLMProvider
from tests.investigation.harness import make_harness

pytestmark = pytest.mark.integration

DB_URL = integration_database_url()
if DB_URL is None:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)


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


async def test_simulated_email_case_in_postgres(
    sessionmaker: async_sessionmaker[AsyncSession], banking: BankingClient
) -> None:
    jobs = JobQueue(sessionmaker)
    harness = make_harness(FakeLLMProvider(), banking, store=SqlInvestigationStore(sessionmaker, jobs))
    view = CustomerView(customer_id=uuid.UUID(ALICE), customer_name="Alice Example", persona=PersonaSpec(knows=["x"]))
    key = AnswerKey(
        issue_type=IssueType.FAILED_DIRECT_DEBIT,
        root_cause=RootCause.MANDATE_CANCELLED_BY_CUSTOMER,
        outcome=ExpectedOutcome.SELF_SERVE,
    )

    case = await create_simulated_email_case(
        harness.service, view, key, "My rent did not go out.", sessionmaker=sessionmaker, jobs=jobs
    )

    body = case.model_dump(mode="json")
    schema_validator(AGENT_URI, "Case", registry=contract_registry()).validate(body)
    assert body["source"] == "simulated_email" and body["simulated"] is True and body["category"] is None
    assert body["active_investigation_id"] is not None and "expected" not in str(body)
    async with sessionmaker() as session:
        row = (await session.execute(select(CaseRow).where(CaseRow.id == case.id))).scalar_one()
        queued = (
            (await session.execute(select(InvestigationRow).where(InvestigationRow.case_id == case.id))).scalars().all()
        )
    assert (row.expected_root_cause, row.expected_outcome) == ("mandate_cancelled_by_customer", "self_serve")
    assert [i.status for i in queued] == ["queued"]
