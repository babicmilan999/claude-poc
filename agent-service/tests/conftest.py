"""Shared fixtures: no network services. The fake LLM, the banking API mocked from the
contract, an in-memory knowledge base, and no database (integration tests excepted)."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.banking import BankingClient
from core.context import AppContext
from core.kb import KnowledgeBaseSearch
from core.kb.embeddings import HashingEmbedder
from core.kb.ingest import ingest_knowledge_base
from core.kb.store import InMemoryKnowledgeBaseStore
from core.settings import Settings
from core.tracing import Tracing
from tests.banking_mock import NOW, ContractBankingMock
from tests.fakes import FakeLLMProvider

FIXTURE_KB = Path(__file__).parent / "fixtures" / "kb"
BANKING_URL = "http://banking.test"


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must not pick up real service settings from the environment."""
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "DATABASE_URL", "BANKING_API_URL", "LITELLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock():
    return lambda: NOW


@pytest.fixture
def settings() -> Settings:
    return Settings(
        banking_api_url=BANKING_URL,
        kb_path=FIXTURE_KB,
        kb_ingest_on_startup=False,
        llm_profile="test",
    )


@pytest.fixture
def banking_mock() -> ContractBankingMock:
    return ContractBankingMock()


@pytest.fixture
async def banking(banking_mock: ContractBankingMock) -> AsyncIterator[BankingClient]:
    client = BankingClient(BANKING_URL, transport=banking_mock.transport)
    yield client
    await client.aclose()


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture
async def kb() -> KnowledgeBaseSearch:
    store = InMemoryKnowledgeBaseStore()
    embedder = HashingEmbedder()
    await ingest_knowledge_base(FIXTURE_KB, store, embedder, base_url="/help/")
    return KnowledgeBaseSearch(store=store, embedder=embedder)


@pytest.fixture
def app_context_factory(banking_mock: ContractBankingMock, fake_llm: FakeLLMProvider):
    def factory(settings: Settings) -> AppContext:
        return AppContext(
            settings=settings,
            banking=BankingClient(settings.banking_api_url, transport=banking_mock.transport),
            llm=fake_llm,
            tracing=Tracing(client=None, profile=settings.llm_profile),
            kb=KnowledgeBaseSearch(store=InMemoryKnowledgeBaseStore(), embedder=HashingEmbedder()),
        )

    return factory


@pytest.fixture
def app_client(settings: Settings, app_context_factory) -> Iterator[TestClient]:
    app = create_app(settings, context_factory=app_context_factory, feature_modules=())
    with TestClient(app) as client:
        yield client


def integration_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")
