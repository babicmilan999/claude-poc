"""Apply the Alembic migrations (`alembic upgrade head`) from code.

The app runs this at startup when `MIGRATE_ON_STARTUP` is true (the default) and
`DATABASE_URL` is set; the manual equivalent is `uv run alembic upgrade head`
(see `agent-service/README.md`).
"""

import logging

from alembic import command
from alembic.config import Config

from core.settings import AGENT_SERVICE_DIR, Settings

log = logging.getLogger(__name__)

ALEMBIC_INI = AGENT_SERVICE_DIR / "alembic.ini"


def should_migrate(settings: Settings) -> bool:
    """Only with `MIGRATE_ON_STARTUP` and an explicitly configured `DATABASE_URL` (never
    against the built-in localhost default, and so never in unit tests)."""
    return settings.migrate_on_startup and "database_url" in settings.model_fields_set and bool(settings.database_url)


def alembic_config(settings: Settings) -> Config:
    config = Config(str(ALEMBIC_INI))
    # `migrations/env.py` reads these: the URL to migrate, and to leave the app's logging alone.
    config.attributes["database_url"] = settings.database_url
    config.attributes["configure_logging"] = False
    return config


def upgrade_head(settings: Settings) -> None:
    """Blocking; call it in a thread from async code."""
    log.info("applying database migrations (alembic upgrade head)")
    command.upgrade(alembic_config(settings), "head")
    log.info("database migrations applied")
