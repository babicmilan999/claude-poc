"""LangGraph checkpointer: Postgres in production (`langgraph-checkpoint-postgres`, its own
tables, created by `setup()` at startup), in memory for unit tests and runs without a
database. The thread id is the investigation id, so a reject-with-hint resumes the same
thread and a crashed run continues from its last checkpoint."""

import contextlib
from collections.abc import AsyncIterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver


def psycopg_url(database_url: str) -> str:
    """`DATABASE_URL` as a plain libpq URL (no SQLAlchemy driver suffix)."""
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


@contextlib.asynccontextmanager
async def postgres_checkpointer(database_url: str, *, max_size: int = 5) -> AsyncIterator[BaseCheckpointSaver]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=psycopg_url(database_url),
        min_size=1,
        max_size=max(1, max_size),
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver
    finally:
        await pool.close()


@contextlib.asynccontextmanager
async def memory_checkpointer() -> AsyncIterator[BaseCheckpointSaver]:
    yield InMemorySaver()
