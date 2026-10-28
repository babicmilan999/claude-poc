"""Alembic environment for the agent tables.

Only the `agents` and `kb` schemas are managed here. The banking schema belongs to the
banking service (Flyway) and the LangGraph checkpointer creates its own tables.
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool, text

from core.db.models import AGENTS_SCHEMA, KB_SCHEMA, Base
from core.settings import Settings

MANAGED_SCHEMAS = {AGENTS_SCHEMA, KB_SCHEMA}
VERSION_TABLE_SCHEMA = AGENTS_SCHEMA

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def _url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return Settings(database_url=override).sqlalchemy_url
    return Settings().sqlalchemy_url


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    if type_ == "table":
        return name != "alembic_version"
    return True


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema=VERSION_TABLE_SCHEMA,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {VERSION_TABLE_SCHEMA}")
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VERSION_TABLE_SCHEMA}"))
        connection.commit()
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
