"""LangGraph checkpointer. The thread id is the investigation id, so a reject-with-hint
resumes the same thread and a crashed run continues from its last checkpoint.

Both graphs share one checkpointer (architecture section 2): `AsyncPostgresSaver` in the
`langgraph` schema, published as `ctx.extras["checkpointer"]`. `core.app` enters the
assistant's lifespan first, so investigations normally reuse the saver (and pool) it
opened; without the assistant package this module opens one in the same schema and
publishes it. Without a database (unit tests): `InMemorySaver`.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from core.context import AppContext

CHECKPOINT_SCHEMA = "langgraph"
SHARED_KEY = "checkpointer"  # the same key as `assistant.checkpoint.SHARED_KEY`


def psycopg_url(database_url: str) -> str:
    """`DATABASE_URL` as a plain libpq URL (no SQLAlchemy driver suffix)."""
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


@contextlib.asynccontextmanager
async def postgres_checkpointer(database_url: str, *, max_size: int = 5) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """An `AsyncPostgresSaver` on its own pool, its tables in the `langgraph` schema."""
    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    url = psycopg_url(database_url)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"')
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=max(1, max_size),
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={CHECKPOINT_SCHEMA}",
        },
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver
    finally:
        await pool.close()


@contextlib.asynccontextmanager
async def memory_checkpointer() -> AsyncIterator[BaseCheckpointSaver[Any]]:
    yield InMemorySaver()


@contextlib.asynccontextmanager
async def shared_checkpointer(
    ctx: AppContext, *, use_postgres: bool, max_size: int = 5
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """The app's shared checkpointer: reused from `ctx.extras["checkpointer"]` when
    another package opened it, otherwise opened here and published until exit."""
    shared = ctx.extras.get(SHARED_KEY)
    if isinstance(shared, BaseCheckpointSaver):
        yield shared
        return
    opened = (
        postgres_checkpointer(ctx.settings.database_url, max_size=max_size) if use_postgres else memory_checkpointer()
    )
    async with opened as saver:
        ctx.extras[SHARED_KEY] = saver
        try:
            yield saver
        finally:
            if ctx.extras.get(SHARED_KEY) is saver:
                del ctx.extras[SHARED_KEY]
