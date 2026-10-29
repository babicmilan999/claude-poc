"""FastAPI app factory.

Feature packages sit beside `core/` (`assistant/`, `investigation/`, `simulation/`) and
plug in without changes here: if `<package>/api.py` exists, the app includes its
`router` and, if defined, enters its `lifespan(app, ctx)` async context manager at
startup (for example to start investigation workers). Their own services can go in
`ctx.extras`.
"""

import asyncio
import contextlib
import importlib
import importlib.util
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from types import ModuleType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.api import router as core_router
from core.context import AppContext, build_context
from core.db.migrate import should_migrate, upgrade_head
from core.kb.ingest import ingest_knowledge_base
from core.settings import Settings, get_settings

log = logging.getLogger(__name__)

FEATURE_MODULES: tuple[str, ...] = ("assistant.api", "investigation.api", "simulation.api")


def load_feature_modules(names: Sequence[str]) -> list[ModuleType]:
    modules: list[ModuleType] = []
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            log.debug("feature module %s not present", name)
            continue
        modules.append(importlib.import_module(name))
    return modules


def problem(
    status: int, title: str, detail: str | None = None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


async def _ingest_kb(ctx: AppContext) -> None:
    try:
        await ingest_knowledge_base(
            ctx.settings.kb_path, ctx.kb.store, ctx.kb.embedder, base_url=ctx.settings.kb_article_base_url
        )
    except Exception:
        log.exception("knowledge base ingestion failed; search may return stale or no results")


def create_app(
    settings: Settings | None = None,
    *,
    context_factory: Callable[[Settings], AppContext] = build_context,
    feature_modules: Sequence[str] = FEATURE_MODULES,
    migrate: Callable[[Settings], None] = upgrade_head,
) -> FastAPI:
    settings = settings or get_settings()
    modules = load_feature_modules(feature_modules)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if should_migrate(settings):
            # Before anything touches the tables; a failure stops the startup.
            await asyncio.to_thread(migrate, settings)
        ctx = context_factory(settings)
        app.state.context = ctx
        ingest_task: asyncio.Task[None] | None = None
        if settings.kb_ingest_on_startup:
            ingest_task = asyncio.create_task(_ingest_kb(ctx), name="kb-ingest")
            app.state.kb_ingest_task = ingest_task
        try:
            async with contextlib.AsyncExitStack() as stack:
                for module in modules:
                    hook = getattr(module, "lifespan", None)
                    if hook is not None:
                        await stack.enter_async_context(hook(app, ctx))
                yield
        finally:
            if ingest_task is not None and not ingest_task.done():
                ingest_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ingest_task
            await ctx.aclose()

    app = FastAPI(
        title="Agent Service API",
        version="0.1.0",
        summary="Customer assistant, cases, investigations and simulation.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Type"],
    )

    # Starlette's HTTPException covers FastAPI's (a subclass) and the router's own 404 for
    # unknown routes and 405 for a wrong method (its `Allow` header is kept).
    @app.exception_handler(StarletteHTTPException)
    async def _http_problem(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = exc.detail if isinstance(exc.detail, str) and exc.detail else "Error"
        return problem(exc.status_code, title, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_problem(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return problem(400, "Invalid request", f"{where}: {first.get('msg', 'invalid')}")

    app.include_router(core_router)
    for module in modules:
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
    return app
