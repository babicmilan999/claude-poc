"""Ops console endpoints for cases and investigations (`contracts/agent-api.yaml`, tags
`ops-cases` and `ops-investigations`), and the lifespan that wires the graph, the
checkpointer, the store and the worker pool.

`core.app` picks this module up: it includes `router` and enters `lifespan(app, ctx)`.
Set `INVESTIGATION_WORKERS_ENABLED=false` to run without the worker pool (tests drive
jobs with `InvestigationService.run_pending()`). Tests can also put a store
(`investigation_store`), a checkpointer (`investigation_checkpointer`) or a clock
(`investigation_clock`) in `ctx.extras`.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from core.banking import BankingError, BankingNotFound
from core.context import AppContext, Context
from core.enums import CaseSource, CaseStatus, IssueType
from core.jobs import CASE_EVENTS_CHANNEL, notify
from investigation.broker import Broker
from investigation.checkpoint import memory_checkpointer, postgres_checkpointer, psycopg_url
from investigation.graph import GraphDeps, build_graph
from investigation.schemas import (
    STREAM_STOP_STATUSES,
    Case,
    CaseDetail,
    CasePage,
    CreateCaseRequest,
    FeedbackRequest,
    Investigation,
    InvestigationEvent,
)
from investigation.service import InvestigationService
from investigation.settings import InvestigationSettings
from investigation.store import Conflict, InvestigationStore, NewCase, NotFound
from investigation.store_memory import MemoryInvestigationStore
from investigation.worker import CaseEventsListener, WorkerPool

log = logging.getLogger(__name__)

router = APIRouter()

SERVICE_KEY = "investigation"
SSE_PING_SECONDS = 15


def get_service(ctx: AppContext) -> InvestigationService:
    service = ctx.extras.get(SERVICE_KEY)
    if service is None:
        raise HTTPException(status_code=503, detail="investigations are starting")
    return service  # type: ignore[no-any-return]


# --- cases --------------------------------------------------------------------------------


def _parse_statuses(raw: str | None) -> list[CaseStatus]:
    if not raw:
        return [s for s in CaseStatus if s is not CaseStatus.RESOLVED]
    try:
        return [CaseStatus(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown case status in {raw!r}") from exc


@router.get("/cases", response_model=CasePage, tags=["ops-cases"])
async def list_cases(
    ctx: Context,
    status: str | None = None,
    source: CaseSource | None = None,
    category: IssueType | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CasePage:
    service = get_service(ctx)
    items, total = await service.store.list_cases(
        statuses=_parse_statuses(status), source=source, category=category, limit=limit, offset=offset
    )
    return CasePage(items=items, total=total)


async def case_event_stream(service: InvestigationService, request: Request) -> AsyncIterator[dict[str, Any]]:
    """Live `CaseEvent`s for one `/cases/events` client (not stored, no replay)."""
    queue = service.broker.subscribe_cases()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_PING_SECONDS)
            except TimeoutError:
                if await request.is_disconnected():
                    return
                continue
            yield {"event": event["type"], "id": event["id"], "data": json.dumps(event, separators=(",", ":"))}
    finally:
        service.broker.unsubscribe_cases(queue)


@router.get("/cases/events", tags=["ops-cases"], response_class=EventSourceResponse)
async def stream_case_events(ctx: Context, request: Request) -> EventSourceResponse:
    service = get_service(ctx)
    return EventSourceResponse(case_event_stream(service, request), ping=SSE_PING_SECONDS)


@router.post("/cases", status_code=201, response_model=Case, tags=["ops-cases"])
async def create_case(ctx: Context, body: CreateCaseRequest) -> Case:
    service = get_service(ctx)
    try:
        customer = await ctx.banking.get_customer(str(body.customer_id))
    except BankingNotFound:
        raise HTTPException(status_code=404, detail="unknown customer") from None
    except BankingError as exc:
        raise HTTPException(status_code=502, detail="banking service unavailable") from exc
    return await service.create_case(
        NewCase(
            customer_id=body.customer_id,
            customer_name=customer.name,
            source=CaseSource(body.source),
            complaint=body.complaint,
            trigger_payload=body.trigger_payload,
        )
    )


@router.get("/cases/{case_id}", response_model=CaseDetail, tags=["ops-cases"])
async def get_case(ctx: Context, case_id: uuid.UUID) -> CaseDetail:
    detail = await get_service(ctx).store.get_case_detail(case_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="case not found")
    return detail


@router.post(
    "/cases/{case_id}/investigate",
    response_model=Investigation,
    tags=["ops-cases"],
    responses={201: {"model": Investigation}},
)
async def investigate_case(ctx: Context, case_id: uuid.UUID, response: Response) -> Investigation:
    try:
        result = await get_service(ctx).start_investigation(case_id, reinvestigate=False)
    except NotFound:
        raise HTTPException(status_code=404, detail="case not found") from None
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    response.status_code = 201 if result.created else 200
    return result.investigation


@router.post("/cases/{case_id}/reinvestigate", status_code=201, response_model=Investigation, tags=["ops-cases"])
async def reinvestigate_case(ctx: Context, case_id: uuid.UUID) -> Investigation:
    try:
        result = await get_service(ctx).start_investigation(case_id, reinvestigate=True)
    except NotFound:
        raise HTTPException(status_code=404, detail="case not found") from None
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return result.investigation


# --- investigations -------------------------------------------------------------------------


@router.get("/investigations/{investigation_id}", response_model=Investigation, tags=["ops-investigations"])
async def get_investigation(ctx: Context, investigation_id: uuid.UUID) -> Investigation:
    investigation = await get_service(ctx).store.get_investigation(investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    return investigation


def _sse(event: InvestigationEvent) -> dict[str, Any]:
    return {"event": event.type, "id": str(event.id), "data": event.model_dump_json()}


async def investigation_event_stream(
    service: InvestigationService, investigation_id: uuid.UUID, after_id: int | None
) -> AsyncIterator[dict[str, Any]]:
    """Stored events after `after_id`, then live ones until the run stops."""
    store, broker = service.store, service.broker
    last = after_id
    poll = service.settings.investigation_stream_poll_seconds
    while True:
        version = broker.version(investigation_id)
        status = await store.investigation_status(investigation_id)
        events = await store.list_events(investigation_id, after_id=last)
        for event in events:
            yield _sse(event)
            last = event.id
        # Status read before the events: everything written up to the stop is sent.
        if status is None or status in STREAM_STOP_STATUSES:
            return
        await broker.wait_investigation(investigation_id, version, poll)


@router.get(
    "/investigations/{investigation_id}/events",
    tags=["ops-investigations"],
    response_model=list[InvestigationEvent],
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def get_investigation_events(
    ctx: Context,
    request: Request,
    investigation_id: uuid.UUID,
    after_id: int | None = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Any:
    service = get_service(ctx)
    if await service.store.investigation_status(investigation_id) is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    after = after_id
    if last_event_id:
        try:
            after = int(last_event_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an event id") from None
    if "text/event-stream" in request.headers.get("accept", ""):
        return EventSourceResponse(investigation_event_stream(service, investigation_id, after), ping=SSE_PING_SECONDS)
    events = await service.store.list_events(investigation_id, after_id=after)
    return JSONResponse([e.model_dump(mode="json") for e in events])


@router.post(
    "/investigations/{investigation_id}/feedback",
    status_code=202,
    response_model=Investigation,
    tags=["ops-investigations"],
)
async def submit_feedback(
    ctx: Context, investigation_id: uuid.UUID, body: FeedbackRequest, background: BackgroundTasks
) -> Investigation:
    service = get_service(ctx)
    try:
        result = await service.submit_feedback(investigation_id, body)
    except NotFound:
        raise HTTPException(status_code=404, detail="investigation not found") from None
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not result.requeued:
        background.add_task(service.finalize, investigation_id, result.resume_payload)
    return result.investigation


# --- wiring ----------------------------------------------------------------------------------


def _case_notifier(ctx: AppContext) -> Any:
    """Publishes case events to other processes (`NOTIFY case_events`). A case_created
    event goes out as its id only (NOTIFY payloads are limited to 8000 bytes)."""
    sessionmaker = ctx.sessionmaker
    assert sessionmaker is not None
    tasks: set[asyncio.Task[None]] = set()

    async def send(text: str) -> None:
        try:
            async with sessionmaker.begin() as session:
                await notify(session, CASE_EVENTS_CHANNEL, text)
        except Exception:
            log.warning("NOTIFY %s failed", CASE_EVENTS_CHANNEL, exc_info=True)

    def notifier(message: dict[str, Any]) -> None:
        event = message.get("event") or {}
        if event.get("type") == "case_created":
            message = {"origin": message.get("origin"), "type": "case_created", "case_id": event["case"]["id"]}
        task = asyncio.create_task(send(json.dumps(message, separators=(",", ":"))))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return notifier


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI, ctx: AppContext) -> AsyncIterator[None]:
    settings = InvestigationSettings()
    use_postgres = ctx.sessionmaker is not None and ctx.jobs is not None
    store: InvestigationStore
    if "investigation_store" in ctx.extras:
        store = ctx.extras["investigation_store"]
    elif use_postgres:
        from investigation.store_sql import SqlInvestigationStore

        assert ctx.sessionmaker is not None and ctx.jobs is not None
        store = SqlInvestigationStore(ctx.sessionmaker, ctx.jobs)
    else:
        store = MemoryInvestigationStore()

    if "investigation_checkpointer" in ctx.extras:
        checkpointer_cm: Any = contextlib.nullcontext(ctx.extras["investigation_checkpointer"])
    elif use_postgres:
        checkpointer_cm = postgres_checkpointer(
            ctx.settings.database_url, max_size=ctx.settings.max_parallel_investigations + 2
        )
    else:
        checkpointer_cm = memory_checkpointer()

    async with checkpointer_cm as checkpointer:
        service_ref: dict[str, InvestigationService] = {}

        def score(investigation_id: str, name: str, value: float, comment: str | None) -> None:
            service_ref["service"].score(investigation_id, name, value, comment)

        deps = GraphDeps(
            llm=ctx.llm,
            banking=ctx.banking,
            load_intake=store.load_intake,
            settings=settings,
            auto_investigate_types=frozenset(ctx.settings.auto_investigate_types),
            triage_threshold=ctx.settings.triage_confidence_threshold,
            score=score,
        )
        if "investigation_clock" in ctx.extras:  # tests: fixed "now" for tool windows
            deps.clock = ctx.extras["investigation_clock"]
        service = InvestigationService(
            store=store,
            graph=build_graph(deps, checkpointer=checkpointer),
            broker=Broker(),
            settings=settings,
            tracing=ctx.tracing,
            case_notifier=_case_notifier(ctx) if use_postgres else None,
        )
        service_ref["service"] = service
        ctx.extras[SERVICE_KEY] = service

        database_url = psycopg_url(ctx.settings.database_url) if use_postgres else None
        listener: CaseEventsListener | None = None
        if database_url:
            listener = CaseEventsListener(service, database_url, CASE_EVENTS_CHANNEL)
            try:
                await listener.start()
            except Exception:
                log.warning("LISTEN %s failed; case events stay in this process", CASE_EVENTS_CHANNEL, exc_info=True)
                listener = None
        pool: WorkerPool | None = None
        if settings.investigation_workers_enabled:
            pool = WorkerPool(service, count=ctx.settings.max_parallel_investigations, database_url=database_url)
            await pool.start()
        try:
            yield
        finally:
            if pool is not None:
                await pool.stop()
            if listener is not None:
                await listener.close()
            await service.wait_background()
            ctx.extras.pop(SERVICE_KEY, None)
