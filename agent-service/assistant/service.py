"""One conversation turn, ratings, and the event hub behind the SSE streams.

A turn runs in a background task, so the graph finishes (and the reply and any case are
saved) even if the customer closes the app mid-stream. Its events go to the POST response
and to the in-process `ChatHub`, which feeds watch mode (`GET /conversations/{id}/events`).

The hub is in-process: watch mode sees conversations whose turns run in the same process
(the customer app and the customer simulator both go through this service).
"""

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from assistant import prompts
from assistant import schemas as ev
from assistant.store import ConversationRecord, ConversationStore, MessageRecord
from core.banking import BankingClient, BankingError
from core.enums import ConversationOutcome, ConversationStatus, ExpectedOutcome, MessageRole, Rating, RootCause
from core.tools.common import Clock, utc_now
from core.tracing import Tracing

log = logging.getLogger(__name__)

_END = object()


class ConversationBusy(Exception):
    """A reply to an earlier message is still being written."""


class ConversationNotOpen(Exception):
    pass


class MessageNotFound(Exception):
    pass


class MessageNotRateable(Exception):
    pass


class ChatHub:
    """In-process publish/subscribe of chat events per conversation."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[Any]]] = defaultdict(set)

    def subscribe(self, conversation_id: uuid.UUID) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers[conversation_id].add(queue)
        return queue

    def unsubscribe(self, conversation_id: uuid.UUID, queue: asyncio.Queue[Any]) -> None:
        subscribers = self._subscribers.get(conversation_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                del self._subscribers[conversation_id]

    def publish(self, conversation_id: uuid.UUID, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(conversation_id, ())):
            queue.put_nowait(event)

    def subscriber_count(self, conversation_id: uuid.UUID) -> int:
        return len(self._subscribers.get(conversation_id, ()))


class AssistantService:
    def __init__(
        self,
        *,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        store: ConversationStore,
        banking: BankingClient,
        tracing: Tracing,
        hub: ChatHub | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.graph = graph
        self.store = store
        self.banking = banking
        self.tracing = tracing
        self.hub = hub or ChatHub()
        self.clock = clock
        self._busy: set[uuid.UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # --- conversations -----------------------------------------------------------------

    async def create_conversation(
        self,
        customer_id: str | uuid.UUID,
        *,
        simulated: bool = False,
        expected_outcome: ExpectedOutcome | None = None,
        expected_root_cause: RootCause | None = None,
    ) -> ConversationRecord:
        """A new open conversation. The answer keys are for simulated conversations
        (stored for scoring only, never shown to the assistant)."""
        cid = uuid.UUID(str(customer_id))
        name: str | None = None
        try:
            name = (await self.banking.get_customer(str(cid))).name
        except BankingError:
            log.warning("customer name for %s not available", cid)
        return await self.store.create_conversation(
            cid,
            name,
            simulated=simulated,
            expected_outcome=expected_outcome,
            expected_root_cause=expected_root_cause,
        )

    async def close_conversation(self, conversation_id: uuid.UUID, outcome: ConversationOutcome) -> ConversationRecord:
        """End an open conversation as `resolved` or `abandoned` (for example when a
        simulated customer leaves, or after it goes quiet)."""
        record = await self.store.update_conversation(
            conversation_id, status=ConversationStatus.CLOSED, outcome=outcome, ended_at=self.clock()
        )
        self._publish(conversation_id, ev.conversation_status(record.status, record.outcome))
        return record

    # --- a turn ------------------------------------------------------------------------

    def is_busy(self, conversation_id: uuid.UUID) -> bool:
        return conversation_id in self._busy

    async def start_turn(self, conversation: ConversationRecord, content: str) -> AsyncIterator[dict[str, Any]]:
        """Save the customer's message and run the graph in the background. Returns the
        turn's events (complete `ChatEvent` objects), ending when the run ends."""
        cid = conversation.id
        if cid in self._busy:
            raise ConversationBusy(str(cid))
        self._busy.add(cid)
        try:
            current = await self.store.get_conversation(cid)
            if current is None or current.status != ConversationStatus.OPEN:
                raise ConversationNotOpen(str(cid))
            message = await self.store.add_message(cid, MessageRole.CUSTOMER, content)
            if current.outcome is not None:
                # A new message after 👍 means it was not resolved after all.
                await self.store.update_conversation(cid, outcome=None)
            down = await self.store.count_message_ratings(cid, Rating.DOWN)
        except BaseException:
            self._busy.discard(cid)
            raise

        queue: asyncio.Queue[Any] = asyncio.Queue()

        def emit(payload: dict[str, Any]) -> None:
            event = ev.chat_event(cid, payload, created_at=self.clock())
            queue.put_nowait(event)
            self.hub.publish(cid, event)

        emit(ev.message_created(message))
        task = asyncio.create_task(self._run(current, message, down, emit, queue), name=f"assistant-turn-{cid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        async def events() -> AsyncIterator[dict[str, Any]]:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                yield item

        return events()

    async def _run(
        self,
        conversation: ConversationRecord,
        message: MessageRecord,
        down_ratings: int,
        emit: Any,
        queue: asyncio.Queue[Any],
    ) -> None:
        cid = conversation.id
        config = self.tracing.config(
            run_name="assistant",
            session_id=str(cid),
            user_id=str(conversation.customer_id),
            tags=["assistant"],
            metadata={"prompt_version": prompts.PROMPT_VERSION, "message_id": message.id},
            configurable={"thread_id": str(cid)},
        )
        state_in = {
            "conversation_id": str(cid),
            "customer_id": str(conversation.customer_id),
            "messages": [HumanMessage(content=message.content, id=f"msg-{message.id}")],
            "down_ratings": down_ratings,
        }
        try:
            async for chunk in self.graph.astream(state_in, config=config, stream_mode="custom"):
                if isinstance(chunk, dict) and "type" in chunk:
                    emit(chunk)
        except Exception:
            log.exception("assistant turn failed in conversation %s", cid)
            emit(ev.error(prompts.ERROR_REPLY))
        finally:
            self._busy.discard(cid)
            queue.put_nowait(_END)
            self.tracing.flush()

    async def run_turn(self, conversation: ConversationRecord, content: str) -> list[dict[str, Any]]:
        """`start_turn` and wait for the whole reply (for the simulator and tests)."""
        return [event async for event in await self.start_turn(conversation, content)]

    def _publish(self, conversation_id: uuid.UUID, payload: dict[str, Any]) -> None:
        self.hub.publish(conversation_id, ev.chat_event(conversation_id, payload, created_at=self.clock()))

    # --- ratings -----------------------------------------------------------------------

    async def rate(self, conversation: ConversationRecord, message_id: int, rating: Rating) -> MessageRecord:
        """👍 resolves the (open) conversation; 👎 counts as "that didn't help" for the
        repeated-failure rule and undoes an earlier 👍's resolution."""
        cid = conversation.id
        message = await self.store.get_message(cid, message_id)
        if message is None:
            raise MessageNotFound(str(message_id))
        if message.role != MessageRole.ASSISTANT:
            raise MessageNotRateable(str(message_id))
        updated = await self.store.set_message_rating(cid, message_id, rating)
        current = await self.store.get_conversation(cid) or conversation
        if current.status == ConversationStatus.OPEN:
            if rating is Rating.UP:
                await self.store.update_conversation(cid, rating=rating, outcome=ConversationOutcome.RESOLVED)
            elif current.outcome == ConversationOutcome.RESOLVED:
                await self.store.update_conversation(cid, rating=rating, outcome=None)
            else:
                await self.store.update_conversation(cid, rating=rating)
        else:
            await self.store.update_conversation(cid, rating=rating)
        self._score_rating(cid, message_id, rating)
        return updated

    def _score_rating(self, conversation_id: uuid.UUID, message_id: int, rating: Rating) -> None:
        client = self.tracing.client
        if client is None:
            return
        try:
            client.create_score(
                name="customer_rating",
                value=1 if rating is Rating.UP else 0,
                session_id=str(conversation_id),
                data_type="BOOLEAN",
                comment=f"message {message_id}",
            )
        except Exception:  # tracing must never break the app
            log.warning("could not record the rating in Langfuse", exc_info=True)

    # --- watch mode --------------------------------------------------------------------

    async def watch(self, conversation_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
        """Replay the stored messages (and the current status), then live events. Ends
        when the conversation closes."""
        queue = self.hub.subscribe(conversation_id)
        try:
            messages = await self.store.list_messages(conversation_id)
            conversation = await self.store.get_conversation(conversation_id)
            if conversation is None:
                return
            seen = {m.id for m in messages}
            for message in messages:
                yield ev.chat_event(conversation_id, ev.message_created(message), event_id=f"replay-msg-{message.id}")
            yield ev.chat_event(
                conversation_id,
                ev.conversation_status(conversation.status, conversation.outcome),
                event_id="replay-status",
            )
            if conversation.status != ConversationStatus.OPEN:
                return
            while True:
                event = await queue.get()
                if _already_replayed(event, seen):
                    continue
                yield event
                if event.get("type") == "conversation_status" and event.get("status") == ConversationStatus.CLOSED:
                    return
        finally:
            self.hub.unsubscribe(conversation_id, queue)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _already_replayed(event: dict[str, Any], seen: set[int]) -> bool:
    if event.get("type") in ("message_created", "message_completed"):
        return event.get("message", {}).get("id") in seen
    if event.get("type") in ("token", "sources"):
        return event.get("message_id") in seen
    return False
