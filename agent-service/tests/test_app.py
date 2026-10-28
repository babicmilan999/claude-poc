"""FastAPI skeleton: core routes, problem responses, feature-module plug-in."""

import contextlib
import sys
import types
from collections.abc import AsyncIterator

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


def test_me_overview_needs_a_valid_customer_header(app_client: TestClient) -> None:
    assert app_client.get("/me/overview").status_code == 400
    assert app_client.get("/me/overview", headers={"X-Customer-Id": "not-a-uuid"}).status_code == 400
    response = app_client.get("/me/overview", headers={"X-Customer-Id": UNKNOWN_CUSTOMER})
    assert response.status_code == 404
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
