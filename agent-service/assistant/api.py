"""Conversation endpoints (`contracts/agent-api.yaml`, tags `customer-chat` and
`shared-conversations`). `core.app` includes `router` and enters `lifespan`.

Customer endpoints take the customer from `X-Customer-Id` (`KnownCustomerId`: 401 when
missing or unknown) and only touch that customer's conversations (404 for anyone else's).
Reads are shared: watch mode and the ops console read any conversation.
"""

import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from sse_starlette import EventSourceResponse, ServerSentEvent

from assistant.checkpoint import open_checkpointer
from assistant.graph import AssistantDeps, build_assistant_graph
from assistant.schemas import (
    ConversationDetailOut,
    ConversationOut,
    ConversationPageOut,
    MessageOut,
    RateMessageRequest,
    SendMessageRequest,
)
from assistant.service import (
    AssistantService,
    ConversationBusy,
    ConversationNotOpen,
    MessageNotFound,
    MessageNotRateable,
)
from assistant.store import ConversationRecord, ConversationStore, InMemoryConversationStore, SqlConversationStore
from core.context import AppContext, Context, KnownCustomerId
from core.enums import ConversationOutcome, ConversationStatus

log = logging.getLogger(__name__)

SERVICE_KEY = "assistant"
STORE_KEY = "assistant_store"  # tests may put a store here before startup

router = APIRouter()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI, ctx: AppContext) -> AsyncIterator[None]:
    store: ConversationStore
    if isinstance(ctx.extras.get(STORE_KEY), InMemoryConversationStore | SqlConversationStore):
        store = ctx.extras[STORE_KEY]
    elif ctx.sessionmaker is not None:
        store = SqlConversationStore(ctx.sessionmaker, ctx.jobs)
    else:
        log.warning("no database: conversations are kept in memory")
        store = InMemoryConversationStore()
    async with open_checkpointer(ctx) as checkpointer:
        deps = AssistantDeps(
            llm=ctx.llm,
            banking=ctx.banking,
            kb=ctx.kb,
            store=store,
            triage_threshold=ctx.settings.triage_confidence_threshold,
        )
        service = AssistantService(
            graph=build_assistant_graph(deps, checkpointer),
            store=store,
            banking=ctx.banking,
            tracing=ctx.tracing,
        )
        ctx.extras[STORE_KEY] = store
        ctx.extras[SERVICE_KEY] = service
        try:
            yield
        finally:
            await service.aclose()
            ctx.extras.pop(SERVICE_KEY, None)


def get_service(ctx: Context) -> AssistantService:
    service = ctx.extras.get(SERVICE_KEY)
    if not isinstance(service, AssistantService):
        raise HTTPException(status_code=503, detail="assistant is starting")
    return service


Service = Annotated[AssistantService, Depends(get_service)]


def _parse_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail="conversation not found") from None


async def _conversation(service: AssistantService, conversation_id: str) -> ConversationRecord:
    record = await service.store.get_conversation(_parse_id(conversation_id))
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return record


async def _own_conversation(service: AssistantService, conversation_id: str, customer_id: str) -> ConversationRecord:
    record = await _conversation(service, conversation_id)
    if str(record.customer_id) != customer_id:
        # Someone else's conversation does not exist for this customer.
        raise HTTPException(status_code=404, detail="conversation not found")
    return record


def _sse(event: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps(event, ensure_ascii=False), event=event["type"])


# --- customer app --------------------------------------------------------------------


@router.post(
    "/conversations",
    status_code=201,
    response_model=ConversationOut,
    tags=["customer-chat"],
    operation_id="createConversation",
)
async def create_conversation(service: Service, customer_id: KnownCustomerId) -> ConversationOut:
    return ConversationOut.of(await service.create_conversation(customer_id))


@router.post(
    "/conversations/{conversation_id}/messages",
    tags=["customer-chat"],
    operation_id="sendMessage",
    response_class=EventSourceResponse,
)
async def send_message(
    conversation_id: str, body: SendMessageRequest, service: Service, customer_id: KnownCustomerId
) -> EventSourceResponse:
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be blank")
    conversation = await _own_conversation(service, conversation_id, customer_id)
    if conversation.status != ConversationStatus.OPEN:
        raise HTTPException(status_code=409, detail="conversation is closed")
    try:
        events = await service.start_turn(conversation, content)
    except ConversationBusy:
        raise HTTPException(status_code=409, detail="a reply is still being written") from None
    except ConversationNotOpen:
        raise HTTPException(status_code=409, detail="conversation is closed") from None

    async def stream() -> AsyncIterator[ServerSentEvent]:
        async for event in events:
            yield _sse(event)

    return EventSourceResponse(stream())


@router.put(
    "/conversations/{conversation_id}/messages/{message_id}/rating",
    response_model=MessageOut,
    tags=["customer-chat"],
    operation_id="rateMessage",
)
async def rate_message(
    conversation_id: str,
    message_id: int,
    body: RateMessageRequest,
    service: Service,
    customer_id: KnownCustomerId,
) -> MessageOut:
    conversation = await _own_conversation(service, conversation_id, customer_id)
    try:
        return MessageOut.of(await service.rate(conversation, message_id, body.rating))
    except MessageNotFound:
        raise HTTPException(status_code=404, detail="message not found") from None
    except MessageNotRateable:
        raise HTTPException(status_code=409, detail="only assistant messages can be rated") from None


# --- shared --------------------------------------------------------------------------


@router.get(
    "/conversations",
    response_model=ConversationPageOut,
    tags=["shared-conversations"],
    operation_id="listConversations",
)
async def list_conversations(
    service: Service,
    status: ConversationStatus | None = None,
    outcome: ConversationOutcome | None = None,
    simulated: bool | None = None,
    customer_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationPageOut:
    items, total = await service.store.list_conversations(
        status=status, outcome=outcome, simulated=simulated, customer_id=customer_id, limit=limit, offset=offset
    )
    return ConversationPageOut(items=[ConversationOut.of(c) for c in items], total=total)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailOut,
    tags=["shared-conversations"],
    operation_id="getConversation",
)
async def get_conversation(conversation_id: str, service: Service) -> ConversationDetailOut:
    record = await _conversation(service, conversation_id)
    messages = await service.store.list_messages(record.id)
    return ConversationDetailOut(
        **ConversationOut.of(record).model_dump(), messages=[MessageOut.of(m) for m in messages]
    )


@router.get(
    "/conversations/{conversation_id}/events",
    tags=["shared-conversations"],
    operation_id="streamConversationEvents",
    response_class=EventSourceResponse,
)
async def stream_conversation_events(conversation_id: str, service: Service) -> EventSourceResponse:
    record = await _conversation(service, conversation_id)

    async def stream() -> AsyncIterator[ServerSentEvent]:
        async for event in service.watch(record.id):
            yield _sse(event)

    return EventSourceResponse(stream())
