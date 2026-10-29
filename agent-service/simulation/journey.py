"""A conversation with the assistant, driven by the simulated customer or a fixed script.

Every customer message goes through `AssistantService.run_turn`, the same path as the
customer app's `POST /conversations/{id}/messages` (input guard, triage, handoff rules,
answer, output guard, and the handoff case). The conversation itself is created by the
caller with `create_conversation(simulated=True, expected_*=...)`, so its answer key is
stored hidden on the conversation (and copied to the case at handoff) without passing
through anything here.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from assistant.service import AssistantService
from assistant.store import ConversationRecord
from core.enums import ConversationOutcome, ConversationStatus, MessageRole
from simulation.persona import ChatLine, SimulatedCustomer

log = logging.getLogger(__name__)

EndedBy = Literal["handoff", "customer_satisfied", "customer_gave_up", "max_turns", "script_end", "closed"]


@dataclass(frozen=True)
class AnswerSample:
    """An answer the assistant sent, with the sources the output guard checked it against
    (for the groundedness judge)."""

    text: str
    sources: list[dict[str, Any]]


@dataclass(frozen=True)
class TurnReply:
    text: str
    handed_off: bool = False
    case_id: uuid.UUID | None = None
    error: str | None = None
    closed: bool = False


@dataclass
class JourneyResult:
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    outcome: ConversationOutcome | None
    ended_by: EndedBy
    case_id: uuid.UUID | None = None
    turns: int = 0
    lines: list[ChatLine] = field(default_factory=list)
    answers: list[AnswerSample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def handed_off(self) -> bool:
        return self.outcome is ConversationOutcome.HANDED_OFF


def reply_from_events(events: Sequence[dict[str, Any]]) -> TurnReply:
    """The assistant's reply in one turn's `ChatEvent`s."""
    texts: list[str] = []
    case_id: uuid.UUID | None = None
    error: str | None = None
    closed = False
    for event in events:
        kind = event.get("type")
        message = event.get("message") or {}
        if kind in ("message_created", "message_completed") and message.get("role") == MessageRole.ASSISTANT.value:
            content = str(message.get("content") or "")
            if content and (not texts or texts[-1] != content):
                texts.append(content)
        elif kind == "handoff":
            case_id = uuid.UUID(str(event["case_id"]))
        elif kind == "error":
            error = str(event.get("message") or "error")
        elif kind == "conversation_status" and event.get("status") == ConversationStatus.CLOSED.value:
            closed = True
    return TurnReply(text="\n".join(texts), handed_off=case_id is not None, case_id=case_id, error=error, closed=closed)


class ConversationDriver:
    """Sends customer messages to the assistant and records what came back."""

    def __init__(self, assistant: AssistantService, conversation: ConversationRecord, *, capture_answers: bool) -> None:
        self.assistant = assistant
        self.conversation = conversation
        self.capture_answers = capture_answers
        self.result = JourneyResult(
            conversation_id=conversation.id,
            customer_id=conversation.customer_id,
            outcome=None,
            ended_by="closed",
        )

    async def send(self, text: str) -> TurnReply:
        events = await self.assistant.run_turn(self.conversation, text)
        reply = reply_from_events(events)
        self.result.turns += 1
        self.result.lines.append(ChatLine("customer", text))
        if reply.text:
            self.result.lines.append(ChatLine("assistant", reply.text))
        if reply.error:
            self.result.errors.append(reply.error)
        if reply.case_id is not None:
            self.result.case_id = reply.case_id
        if self.capture_answers:
            await self._capture()
        return reply

    async def _capture(self) -> None:
        graph = self.assistant.graph
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(self.conversation.id)}})
        values = snapshot.values or {}
        if values.get("result") == "answered" and values.get("draft"):
            self.result.answers.append(
                AnswerSample(text=str(values["draft"]), sources=list(values.get("guard_sources") or []))
            )

    async def finish(self, ended_by: EndedBy, outcome: ConversationOutcome | None = None) -> JourneyResult:
        """Close the conversation (if still open) and return the result."""
        current = await self.assistant.store.get_conversation(self.conversation.id)
        if current is not None and current.status == ConversationStatus.OPEN and outcome is not None:
            current = await self.assistant.close_conversation(self.conversation.id, outcome)
        self.result.ended_by = ended_by
        self.result.outcome = current.outcome if current is not None else outcome
        if current is not None and current.case_id is not None:
            self.result.case_id = current.case_id
        return self.result


async def run_simulated_conversation(
    assistant: AssistantService,
    conversation: ConversationRecord,
    customer: SimulatedCustomer,
    *,
    max_turns: int,
    capture_answers: bool = False,
) -> JourneyResult:
    """The simulated customer chats until the assistant hands off, the customer is done,
    or `max_turns` customer messages were sent. A customer who stops is `resolved` when
    satisfied, else `abandoned`; running out of turns is `abandoned`."""
    driver = ConversationDriver(assistant, conversation, capture_answers=capture_answers)
    limit = max(1, min(max_turns, customer.view.persona.max_turns))
    while True:
        turn = await customer.next_turn(driver.result.lines)
        if turn.done:
            if turn.satisfied and driver.result.turns > 0:
                return await driver.finish("customer_satisfied", ConversationOutcome.RESOLVED)
            return await driver.finish("customer_gave_up", ConversationOutcome.ABANDONED)
        if driver.result.turns >= limit:
            return await driver.finish("max_turns", ConversationOutcome.ABANDONED)
        reply = await driver.send(turn.message)
        if reply.handed_off:
            return await driver.finish("handoff")
        if reply.closed:
            return await driver.finish("closed")


async def run_scripted_conversation(
    assistant: AssistantService,
    conversation: ConversationRecord,
    script: Sequence[str],
    *,
    capture_answers: bool = False,
) -> JourneyResult:
    """Fixed customer turns (repeatable assistant evals). Stops at a handoff. A script that
    ends without one leaves the conversation `abandoned`: whether the customer was helped
    is not known, only that the assistant did not hand off."""
    driver = ConversationDriver(assistant, conversation, capture_answers=capture_answers)
    for text in script:
        reply = await driver.send(text)
        if reply.handed_off:
            return await driver.finish("handoff")
        if reply.closed:
            return await driver.finish("closed")
    return await driver.finish("script_end", ConversationOutcome.ABANDONED)
