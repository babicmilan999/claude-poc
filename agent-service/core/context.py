"""The shared services the API routes and graphs use, built once per app.

Routes get it with `Depends(get_context)`. Tests build one with fakes (fake LLM, banking
API mocked from the contract, in-memory knowledge base, no database).
"""

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.banking import BankingClient
from core.jobs import JobQueue
from core.kb import KnowledgeBaseSearch
from core.llm import LiteLLMProvider, LLMProvider
from core.settings import Settings
from core.tracing import Tracing


@dataclass
class AppContext:
    settings: Settings
    banking: BankingClient
    llm: LLMProvider
    tracing: Tracing
    kb: KnowledgeBaseSearch
    engine: AsyncEngine | None = None
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    jobs: JobQueue | None = None
    # Room for services added by the assistant / investigation / simulation packages.
    extras: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.banking.aclose()
        self.tracing.shutdown()
        if self.engine is not None:
            await self.engine.dispose()


def build_context(settings: Settings) -> AppContext:
    """The production wiring. Nothing here connects yet."""
    from core.db.session import create_engine, make_sessionmaker
    from core.kb.embeddings import FastEmbedEmbedder
    from core.kb.store import PostgresKnowledgeBaseStore

    engine = create_engine(settings)
    sessionmaker = make_sessionmaker(engine)
    return AppContext(
        settings=settings,
        banking=BankingClient(settings.banking_api_url, timeout=settings.banking_timeout_seconds),
        llm=LiteLLMProvider(settings),
        tracing=Tracing.from_settings(settings),
        kb=KnowledgeBaseSearch(
            store=PostgresKnowledgeBaseStore(sessionmaker),
            embedder=FastEmbedEmbedder(settings.embedding_model),
        ),
        engine=engine,
        sessionmaker=sessionmaker,
        jobs=JobQueue(sessionmaker),
    )


def get_context(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "context", None)
    if ctx is None:
        raise HTTPException(status_code=503, detail="service is starting")
    return ctx  # type: ignore[no-any-return]


def get_session_customer_id(
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-Id")] = None,
) -> str:
    """The logged-in customer from the `X-Customer-Id` header (dev-only "log in as").
    This is the only source of `customer_id` for customer-scoped tools."""
    if not x_customer_id:
        raise HTTPException(status_code=400, detail="X-Customer-Id header is required")
    try:
        return str(uuid.UUID(x_customer_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Customer-Id must be a UUID") from None


Context = Annotated[AppContext, Depends(get_context)]
SessionCustomerId = Annotated[str, Depends(get_session_customer_id)]
