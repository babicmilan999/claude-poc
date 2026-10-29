"""The LangGraph checkpointer for assistant threads.

With a database: `AsyncPostgresSaver` in the `langgraph` schema (architecture section 2),
on its own small psycopg pool. Without one (unit tests): `InMemorySaver`.

The saver is shared through `ctx.extras["checkpointer"]`: if another package has already
opened one, it is reused; otherwise the one opened here is published there.
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from core.context import AppContext

log = logging.getLogger(__name__)

CHECKPOINT_SCHEMA = "langgraph"
SHARED_KEY = "checkpointer"


def libpq_url(database_url: str) -> str:
    """`DATABASE_URL` without a SQLAlchemy driver suffix, for psycopg."""
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


@contextlib.asynccontextmanager
async def open_checkpointer(ctx: AppContext) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    shared = ctx.extras.get(SHARED_KEY)
    if isinstance(shared, BaseCheckpointSaver):
        yield shared
        return
    if ctx.sessionmaker is None:
        saver: BaseCheckpointSaver[Any] = InMemorySaver()
        ctx.extras[SHARED_KEY] = saver
        yield saver
        return

    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    url = libpq_url(ctx.settings.database_url)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"')
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=5,
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
        ctx.extras[SHARED_KEY] = saver
        yield saver
    finally:
        if ctx.extras.get(SHARED_KEY) is saver:
            del ctx.extras[SHARED_KEY]
        await pool.close()
