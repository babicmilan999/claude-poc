"""DB models and the migration, checked offline (no Postgres)."""

import io
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from core.db.models import Base
from core.jobs import claim_statement
from core.settings import AGENT_SERVICE_DIR

EXPECTED_TABLES = {
    "agents.conversations",
    "agents.messages",
    "agents.cases",
    "agents.investigations",
    "agents.investigation_events",
    "agents.diagnoses",
    "agents.reviews",
    "kb.kb_chunks",
}


def _alembic_config() -> Config:
    config = Config(str(AGENT_SERVICE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(AGENT_SERVICE_DIR / "migrations"))
    return config


def test_models_define_the_section_9_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_single_migration_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == ["0001"]


def test_migration_renders_offline() -> None:
    """`alembic upgrade head --sql` works without a database and creates every table,
    the pgvector extension, the queue index and the one-active-investigation rule."""
    buffer = io.StringIO()
    config = _alembic_config()
    config.cmd_opts = None
    config.output_buffer = buffer
    with redirect_stdout(io.StringIO()):
        command.upgrade(config, "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} " in sql, table
    assert "CREATE UNIQUE INDEX uq_investigations_one_active_per_case" in sql
    assert "WHERE status IN ('queued', 'running', 'awaiting_review')" in sql
    assert "ix_investigations_queue" in sql and "WHERE status = 'queued'" in sql
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "VECTOR(384)" in sql
    assert "agents.alembic_version" in sql


def test_migration_matches_models_columns() -> None:
    """Every model column appears in the migration file (the integration test compares
    the migrated schema with the models fully)."""
    source = Path(AGENT_SERVICE_DIR / "migrations" / "versions" / "0001_agent_tables.py").read_text()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert f'"{column.name}"' in source, f"{table.fullname}.{column.name} missing from migration"


def test_claim_uses_skip_locked() -> None:
    sql = str(claim_statement("worker-1").compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "ORDER BY agents.investigations.queued_at" in sql
    assert "RETURNING" in sql
