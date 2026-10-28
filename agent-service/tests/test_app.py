"""FastAPI skeleton: core routes, problem responses, feature-module plug-in."""

import contextlib
import sys
import types
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.app import create_app, load_feature_modules
from core.context import AppContext
from core.settings import Settings
from tests.banking_mock import AGENT_URI, ALICE, UNKNOWN_CUSTOMER, ContractBankingMock, schema_validator


def test_health(app_client: TestClient) -> None:
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "llm_profile": "test"}


def test_customers(app_client: TestClient) -> None:
    response = app_client.get("/customers")
    assert response.status_code == 200
    body = response.json()
    assert {c["name"] for c in body} == {"Alice Example", "Bob Example"}
    validator = schema_validator(AGENT_URI, "CustomerSummary")
    for item in body:
        validator.validate(item)


def test_customers_banking_down(app_client: TestClient, banking_mock: ContractBankingMock) -> None:
    banking_mock.fail_with["listCustomers"] = 500
    response = app_client.get("/customers")
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 502


def test_me_overview(app_client: TestClient) -> None:
    response = app_client.get("/me/overview", headers={"X-Customer-Id": ALICE})
    assert response.status_code == 200
    body = response.json()
    schema_validator(AGENT_URI, "CustomerOverview").validate(body)
    assert body["accounts"][0]["masked_account_number"] == "****5678"
    assert "12345678" not in response.text and "12-34-56" not in response.text
    assert [c["status"] for c in body["cards"]] == ["frozen"]
    assert "FROZEN_BY_CUSTOMER" not in response.text
    assert [t["description"] for t in body["recent_transactions"]] == ["Coffee House", "Salary"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Customer-Id": ""}, {"X-Customer-Id": "not-a-uuid"}, {"X-Customer-Id": UNKNOWN_CUSTOMER}],
    ids=["missing", "empty", "not-a-uuid", "unknown"],
)
def test_me_overview_without_a_valid_customer_is_401(app_client: TestClient, headers: dict[str, str]) -> None:
    """The contract declares only 200 and 401 for /me/overview."""
    response = app_client.get("/me/overview", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 401


def test_me_overview_banking_down_is_502(app_client: TestClient, banking_mock: ContractBankingMock) -> None:
    """Not in the contract yet (recorded as a contract gap); a problem response all the same."""
    banking_mock.fail_with["getCustomer"] = 500
    response = app_client.get("/me/overview", headers={"X-Customer-Id": ALICE})
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")


def test_cors_for_the_front_ends(app_client: TestClient) -> None:
    response = app_client.options(
        "/health", headers={"Origin": "http://localhost:4300", "Access-Control-Request-Method": "GET"}
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4300"


def test_missing_feature_modules_are_skipped() -> None:
    assert load_feature_modules(("no_such_pkg.api", "core.no_such_module")) == []


def test_feature_modules_plug_in_routes_and_lifespan(settings: Settings, app_context_factory) -> None:
    """A package beside core/ (e.g. investigation/) adds routes and startup work."""
    events: list[str] = []
    router = APIRouter()

    @router.get("/cases")
    async def cases() -> list[str]:
        return ["case-1"]

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI, ctx: AppContext) -> AsyncIterator[None]:
        events.append("start")
        ctx.extras["workers"] = "running"
        yield
        events.append("stop")

    package = types.ModuleType("fake_feature")
    package.__path__ = []  # type: ignore[attr-defined]
    module = types.ModuleType("fake_feature.api")
    module.router = router  # type: ignore[attr-defined]
    module.lifespan = lifespan  # type: ignore[attr-defined]
    module.__spec__ = types.SimpleNamespace(name="fake_feature.api")  # type: ignore[assignment]
    sys.modules["fake_feature"] = package
    sys.modules["fake_feature.api"] = module
    try:
        app = create_app(settings, context_factory=app_context_factory, feature_modules=("fake_feature.api",))
        with TestClient(app) as client:
            assert client.get("/cases").json() == ["case-1"]
            assert app.state.context.extras["workers"] == "running"
            assert events == ["start"]
        assert events == ["start", "stop"]
    finally:
        sys.modules.pop("fake_feature.api", None)
        sys.modules.pop("fake_feature", None)


def test_main_module_builds_the_app(monkeypatch) -> None:
    """`main:app` builds without connecting to anything."""
    import importlib

    monkeypatch.setenv("KB_INGEST_ON_STARTUP", "false")
    from core.settings import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("main")
    assert isinstance(main.app, FastAPI)
    assert {"/health", "/customers", "/me/overview"} <= set(main.app.openapi()["paths"])
    get_settings.cache_clear()


# --- startup migrations ------------------------------------------------------------------


def test_startup_runs_migrations_before_kb_ingestion(settings: Settings, app_context_factory, monkeypatch) -> None:
    import core.app as app_module

    events: list[str] = []

    async def fake_ingest(ctx: AppContext) -> None:
        events.append("ingest")

    monkeypatch.setattr(app_module, "_ingest_kb", fake_ingest)
    configured = Settings(
        **{**settings.model_dump(), "database_url": "postgresql://app:app@db:5432/app", "kb_ingest_on_startup": True}
    )
    assert configured.migrate_on_startup
    app = create_app(
        configured,
        context_factory=app_context_factory,
        feature_modules=(),
        migrate=lambda s: events.append(f"migrate {s.database_url}"),
    )
    with TestClient(app) as client:
        client.get("/health")
        app.state.kb_ingest_task.result()  # ingestion has run
    assert events == ["migrate postgresql://app:app@db:5432/app", "ingest"]


@pytest.mark.parametrize(
    "overrides",
    [{"migrate_on_startup": False, "database_url": "postgresql://app:app@db:5432/app"}, {}],
    ids=["disabled", "no-database-url"],
)
def test_startup_skips_migrations(settings: Settings, app_context_factory, overrides: dict[str, object]) -> None:
    calls: list[Settings] = []
    configured = Settings(**{**settings.model_dump(), **overrides}) if overrides else settings
    app = create_app(configured, context_factory=app_context_factory, feature_modules=(), migrate=calls.append)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert calls == []


def test_startup_fails_when_migrations_fail(settings: Settings, app_context_factory) -> None:
    configured = Settings(**{**settings.model_dump(), "database_url": "postgresql://app:app@db:5432/app"})

    def broken(_: Settings) -> None:
        raise RuntimeError("database unreachable")

    app = create_app(configured, context_factory=app_context_factory, feature_modules=(), migrate=broken)
    with pytest.raises(RuntimeError, match="database unreachable"), TestClient(app):
        pass


def test_alembic_config_targets_the_service_migrations() -> None:
    from core.db.migrate import alembic_config
    from core.settings import AGENT_SERVICE_DIR

    config = alembic_config(Settings(database_url="postgresql://app:app@db:5432/app"))
    assert config.get_main_option("script_location") == str(AGENT_SERVICE_DIR / "migrations")
    assert config.attributes["database_url"] == "postgresql://app:app@db:5432/app"


def test_migrations_render_offline_with_the_startup_config(capsys) -> None:
    """`env.py` takes the URL from the startup config; offline (`--sql`) mode needs no database."""
    from alembic import command

    from core.db.migrate import alembic_config

    command.upgrade(alembic_config(Settings(database_url="postgresql://app:app@db:5432/app")), "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE agents.investigations" in sql
