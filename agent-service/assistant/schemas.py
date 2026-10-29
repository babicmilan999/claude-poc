"""Response bodies and chat events, shaped like `contracts/agent-api.yaml`.

No model here has a field for a simulation answer key.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant.store import ConversationRecord, MessageRecord
from core.enums import ConversationOutcome, ConversationStatus, IssueType, MessageRole, Rating


def _none(value: Any) -> bool:
    return value is None


class SourceOut(BaseModel):
    kind: Literal["article", "account_data"]
    title: str
    article_id: str | None = Field(default=None, exclude_if=_none)
    url: str | None = Field(default=None, exclude_if=_none)
    tool: str | None = Field(default=None, exclude_if=_none)


class MessageOut(BaseModel):
    id: int
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    sources: list[SourceOut]
    rating: Rating | None = None
    created_at: datetime

    @classmethod
    def of(cls, record: MessageRecord) -> "MessageOut":
        return cls(
            id=record.id,
            conversation_id=record.conversation_id,
            role=record.role,
            content=record.content,
            sources=[SourceOut.model_validate(s) for s in record.sources],
            rating=record.rating,
            created_at=record.created_at,
        )


class ConversationOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None = Field(default=None, exclude_if=_none)
    status: ConversationStatus
    outcome: ConversationOutcome | None = None
    category: IssueType | None = None
    rating: Rating | None = None
    simulated: bool
    case_id: uuid.UUID | None = None
    started_at: datetime
    ended_at: datetime | None = None

    @classmethod
    def of(cls, record: ConversationRecord) -> "ConversationOut":
        return cls(
            id=record.id,
            customer_id=record.customer_id,
            customer_name=record.customer_name,
            status=record.status,
            outcome=record.outcome,
            category=record.category,
            rating=record.rating,
            simulated=record.simulated,
            case_id=record.case_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
        )


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut]


class ConversationPageOut(BaseModel):
    items: list[ConversationOut]
    total: int


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class RateMessageRequest(BaseModel):
    rating: Rating


# --- chat events (`ChatEvent`) -------------------------------------------------------
# Graph nodes emit the type-specific part; `chat_event()` adds id, conversation_id and
# created_at.


def message_json(record: MessageRecord) -> dict[str, Any]:
    return MessageOut.of(record).model_dump(mode="json")


def timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def chat_event(
    conversation_id: uuid.UUID | str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id or uuid.uuid4().hex,
        "conversation_id": str(conversation_id),
        "created_at": timestamp(created_at),
        **payload,
    }


def message_created(record: MessageRecord) -> dict[str, Any]:
    return {"type": "message_created", "message": message_json(record)}


def message_completed(record: MessageRecord) -> dict[str, Any]:
    return {"type": "message_completed", "message": message_json(record)}


def token(message_id: int, text: str) -> dict[str, Any]:
    return {"type": "token", "message_id": message_id, "text": text}


def sources(message_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "sources",
        "message_id": message_id,
        "sources": [SourceOut.model_validate(s).model_dump(mode="json") for s in items],
    }


def handoff(case_id: uuid.UUID, notice: str) -> dict[str, Any]:
    return {"type": "handoff", "case_id": str(case_id), "notice": notice}


def conversation_status(status: ConversationStatus, outcome: ConversationOutcome | None) -> dict[str, Any]:
    return {
        "type": "conversation_status",
        "status": status.value,
        "outcome": outcome.value if outcome is not None else None,
    }


def error(message: str) -> dict[str, Any]:
    return {"type": "error", "message": message}
