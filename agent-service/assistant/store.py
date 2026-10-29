"""Conversations, messages and handoff cases.

`SqlConversationStore` writes the `agents` tables (`core.db.models`); `InMemoryConversationStore`
does the same in memory, for unit tests and running without a database.

Simulation answer keys (`expected_root_cause`, `expected_outcome`) are written here when a
simulated conversation starts and copied onto the case at handoff, for scoring. They never
appear in a record this module returns, so no graph, tool, prompt or API response sees them.
"""

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Case, Conversation, Message
from core.enums import (
    CaseSource,
    CaseStatus,
    ConversationOutcome,
    ConversationStatus,
    ExpectedOutcome,
    InvestigationStatus,
    InvestigationTrigger,
    IssueType,
    MessageRole,
    Rating,
    RootCause,
)
from core.jobs import CASE_EVENTS_CHANNEL, JobQueue, notify
from core.tools.common import Clock, utc_now

log = logging.getLogger(__name__)


class ConversationNotFound(Exception):
    pass


class ConversationClosed(Exception):
    pass


@dataclass(frozen=True)
class ConversationRecord:
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None
    status: ConversationStatus
    outcome: ConversationOutcome | None
    category: IssueType | None
    rating: Rating | None
    simulated: bool
    case_id: uuid.UUID | None
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class MessageRecord:
    id: int
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    sources: list[dict[str, Any]]
    rating: Rating | None
    created_at: datetime


@dataclass(frozen=True)
class NewHandoffCase:
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None
    category: IssueType
    complaint: str
    summary: str
    evidence: list[dict[str, Any]]
    notice: str


@dataclass(frozen=True)
class HandoffResult:
    case_id: uuid.UUID
    investigation_id: uuid.UUID | None
    notice: MessageRecord
    conversation: ConversationRecord


class _Unset:
    pass


UNSET: Any = _Unset()


class ConversationStore(Protocol):
    async def create_conversation(
        self,
        customer_id: uuid.UUID,
        customer_name: str | None,
        *,
        simulated: bool = False,
        expected_outcome: ExpectedOutcome | None = None,
        expected_root_cause: RootCause | None = None,
    ) -> ConversationRecord: ...

    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationRecord | None: ...

    async def list_conversations(
        self,
        *,
        status: ConversationStatus | None = None,
        outcome: ConversationOutcome | None = None,
        simulated: bool | None = None,
        customer_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ConversationRecord], int]: ...

    async def update_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        status: ConversationStatus = UNSET,
        outcome: ConversationOutcome | None = UNSET,
        category: IssueType | None = UNSET,
        rating: Rating | None = UNSET,
        ended_at: datetime | None = UNSET,
    ) -> ConversationRecord: ...

    async def list_messages(self, conversation_id: uuid.UUID) -> list[MessageRecord]: ...

    async def get_message(self, conversation_id: uuid.UUID, message_id: int) -> MessageRecord | None: ...

    async def add_message(
        self, conversation_id: uuid.UUID, role: MessageRole, content: str, sources: Sequence[dict[str, Any]] = ()
    ) -> MessageRecord: ...

    async def set_message_rating(
        self, conversation_id: uuid.UUID, message_id: int, rating: Rating
    ) -> MessageRecord: ...

    async def count_message_ratings(self, conversation_id: uuid.UUID, rating: Rating) -> int: ...

    async def create_handoff_case(self, new: NewHandoffCase) -> HandoffResult:
        """In one transaction: the case (`source = assistant_handoff`, status `new`), its
        `queued` investigation, the handoff notice message, and the conversation closed
        as `handed_off`; then `NOTIFY` the workers and the case queue."""
        ...


def _updates(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not UNSET}


def case_created_payload(case_id: uuid.UUID) -> str:
    """The `NOTIFY case_events` payload sent when a case is created."""
    return json.dumps({"type": "case_created", "case_id": str(case_id)})


# --- in memory ------------------------------------------------------------------------


@dataclass
class _StoredConversation:
    record: ConversationRecord
    expected_outcome: ExpectedOutcome | None = None
    expected_root_cause: RootCause | None = None
    messages: list[MessageRecord] = field(default_factory=list)


class InMemoryConversationStore:
    """Same behaviour as the SQL store. `cases` and `investigations` are exposed for tests."""

    def __init__(self, *, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._conversations: dict[uuid.UUID, _StoredConversation] = {}
        self._next_message_id = 1
        self.cases: list[dict[str, Any]] = []
        self.investigations: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, str]] = []

    def _get(self, conversation_id: uuid.UUID) -> _StoredConversation:
        stored = self._conversations.get(conversation_id)
        if stored is None:
            raise ConversationNotFound(str(conversation_id))
        return stored

    async def create_conversation(
        self,
        customer_id: uuid.UUID,
        customer_name: str | None,
        *,
        simulated: bool = False,
        expected_outcome: ExpectedOutcome | None = None,
        expected_root_cause: RootCause | None = None,
    ) -> ConversationRecord:
        record = ConversationRecord(
            id=uuid.uuid4(),
            customer_id=customer_id,
            customer_name=customer_name,
            status=ConversationStatus.OPEN,
            outcome=None,
            category=None,
            rating=None,
            simulated=simulated,
            case_id=None,
            started_at=self._clock(),
            ended_at=None,
        )
        self._conversations[record.id] = _StoredConversation(record, expected_outcome, expected_root_cause)
        return record

    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationRecord | None:
        stored = self._conversations.get(conversation_id)
        return stored.record if stored else None

    async def list_conversations(
        self,
        *,
        status: ConversationStatus | None = None,
        outcome: ConversationOutcome | None = None,
        simulated: bool | None = None,
        customer_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ConversationRecord], int]:
        items = [s.record for s in self._conversations.values()]
        if status is not None:
            items = [c for c in items if c.status == status]
        if outcome is not None:
            items = [c for c in items if c.outcome == outcome]
        if simulated is not None:
            items = [c for c in items if c.simulated == simulated]
        if customer_id is not None:
            items = [c for c in items if c.customer_id == customer_id]
        items.sort(key=lambda c: c.started_at, reverse=True)
        return items[offset : offset + limit], len(items)

    async def update_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        status: ConversationStatus = UNSET,
        outcome: ConversationOutcome | None = UNSET,
        category: IssueType | None = UNSET,
        rating: Rating | None = UNSET,
        ended_at: datetime | None = UNSET,
    ) -> ConversationRecord:
        stored = self._get(conversation_id)
        stored.record = replace(
            stored.record,
            **_updates(status=status, outcome=outcome, category=category, rating=rating, ended_at=ended_at),
        )
        return stored.record

    async def list_messages(self, conversation_id: uuid.UUID) -> list[MessageRecord]:
        return list(self._get(conversation_id).messages)

    async def get_message(self, conversation_id: uuid.UUID, message_id: int) -> MessageRecord | None:
        stored = self._conversations.get(conversation_id)
        if stored is None:
            return None
        return next((m for m in stored.messages if m.id == message_id), None)

    async def add_message(
        self, conversation_id: uuid.UUID, role: MessageRole, content: str, sources: Sequence[dict[str, Any]] = ()
    ) -> MessageRecord:
        stored = self._get(conversation_id)
        message = MessageRecord(
            id=self._next_message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            sources=[dict(s) for s in sources],
            rating=None,
            created_at=self._clock(),
        )
        self._next_message_id += 1
        stored.messages.append(message)
        return message

    async def set_message_rating(self, conversation_id: uuid.UUID, message_id: int, rating: Rating) -> MessageRecord:
        stored = self._get(conversation_id)
        for i, message in enumerate(stored.messages):
            if message.id == message_id:
                stored.messages[i] = replace(message, rating=rating)
                return stored.messages[i]
        raise ConversationNotFound(f"message {message_id}")

    async def count_message_ratings(self, conversation_id: uuid.UUID, rating: Rating) -> int:
        return sum(1 for m in self._get(conversation_id).messages if m.rating == rating)

    async def create_handoff_case(self, new: NewHandoffCase) -> HandoffResult:
        stored = self._get(new.conversation_id)
        if stored.record.status != ConversationStatus.OPEN:
            raise ConversationClosed(str(new.conversation_id))
        case_id = uuid.uuid4()
        investigation_id = uuid.uuid4()
        now = self._clock()
        self.cases.append(
            {
                "id": case_id,
                "customer_id": new.customer_id,
                "customer_name": new.customer_name or stored.record.customer_name,
                "conversation_id": new.conversation_id,
                "source": CaseSource.ASSISTANT_HANDOFF,
                "category": new.category,
                "complaint": new.complaint,
                "summary": new.summary,
                "handoff_evidence": list(new.evidence),
                "status": CaseStatus.NEW,
                "simulated": stored.record.simulated,
                "expected_root_cause": stored.expected_root_cause,
                "expected_outcome": stored.expected_outcome,
                "created_at": now,
            }
        )
        self.investigations.append(
            {
                "id": investigation_id,
                "case_id": case_id,
                "trigger": InvestigationTrigger.AUTO,
                "status": InvestigationStatus.QUEUED,
            }
        )
        notice = await self.add_message(new.conversation_id, MessageRole.ASSISTANT, new.notice)
        stored.record = replace(
            stored.record,
            status=ConversationStatus.CLOSED,
            outcome=ConversationOutcome.HANDED_OFF,
            category=new.category,
            case_id=case_id,
            ended_at=now,
        )
        self.notifications.append(("investigation_jobs", str(investigation_id)))
        self.notifications.append((CASE_EVENTS_CHANNEL, case_created_payload(case_id)))
        return HandoffResult(case_id, investigation_id, notice, stored.record)


# --- Postgres -------------------------------------------------------------------------


def _case_id_column() -> Any:
    return (
        select(Case.id)
        .where(Case.conversation_id == Conversation.id)
        .order_by(Case.created_at.desc())
        .limit(1)
        .scalar_subquery()
        .label("case_id")
    )


def _conversation_record(row: Conversation, case_id: uuid.UUID | None) -> ConversationRecord:
    return ConversationRecord(
        id=row.id,
        customer_id=row.customer_id,
        customer_name=row.customer_name,
        status=ConversationStatus(row.status),
        outcome=ConversationOutcome(row.outcome) if row.outcome else None,
        category=IssueType(row.category) if row.category else None,
        rating=Rating(row.rating) if row.rating else None,
        simulated=row.simulated,
        case_id=case_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _message_record(row: Message) -> MessageRecord:
    return MessageRecord(
        id=row.id,
        conversation_id=row.conversation_id,
        role=MessageRole(row.role),
        content=row.content,
        sources=list(row.sources or []),
        rating=Rating(row.rating) if row.rating else None,
        created_at=row.created_at,
    )


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


class SqlConversationStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], jobs: JobQueue | None) -> None:
        self._sessionmaker = sessionmaker
        self._jobs = jobs

    async def _record(self, session: AsyncSession, conversation_id: uuid.UUID) -> ConversationRecord | None:
        row = (
            await session.execute(select(Conversation, _case_id_column()).where(Conversation.id == conversation_id))
        ).first()
        return _conversation_record(row[0], row[1]) if row else None

    async def create_conversation(
        self,
        customer_id: uuid.UUID,
        customer_name: str | None,
        *,
        simulated: bool = False,
        expected_outcome: ExpectedOutcome | None = None,
        expected_root_cause: RootCause | None = None,
    ) -> ConversationRecord:
        async with self._sessionmaker.begin() as session:
            row = Conversation(
                id=uuid.uuid4(),
                customer_id=customer_id,
                customer_name=customer_name,
                status=ConversationStatus.OPEN.value,
                simulated=simulated,
                expected_outcome=_enum_value(expected_outcome),
                expected_root_cause=_enum_value(expected_root_cause),
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _conversation_record(row, None)

    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationRecord | None:
        async with self._sessionmaker() as session:
            return await self._record(session, conversation_id)

    async def list_conversations(
        self,
        *,
        status: ConversationStatus | None = None,
        outcome: ConversationOutcome | None = None,
        simulated: bool | None = None,
        customer_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ConversationRecord], int]:
        conditions = []
        if status is not None:
            conditions.append(Conversation.status == status.value)
        if outcome is not None:
            conditions.append(Conversation.outcome == outcome.value)
        if simulated is not None:
            conditions.append(Conversation.simulated == simulated)
        if customer_id is not None:
            conditions.append(Conversation.customer_id == customer_id)
        async with self._sessionmaker() as session:
            total = (await session.execute(select(func.count(Conversation.id)).where(*conditions))).scalar_one()
            rows = (
                await session.execute(
                    select(Conversation, _case_id_column())
                    .where(*conditions)
                    .order_by(Conversation.started_at.desc(), Conversation.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return [_conversation_record(r[0], r[1]) for r in rows], int(total)

    async def update_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        status: ConversationStatus = UNSET,
        outcome: ConversationOutcome | None = UNSET,
        category: IssueType | None = UNSET,
        rating: Rating | None = UNSET,
        ended_at: datetime | None = UNSET,
    ) -> ConversationRecord:
        values = {
            k: _enum_value(v)
            for k, v in _updates(
                status=status, outcome=outcome, category=category, rating=rating, ended_at=ended_at
            ).items()
        }
        async with self._sessionmaker.begin() as session:
            if values:
                await session.execute(update(Conversation).where(Conversation.id == conversation_id).values(**values))
            record = await self._record(session, conversation_id)
        if record is None:
            raise ConversationNotFound(str(conversation_id))
        return record

    async def list_messages(self, conversation_id: uuid.UUID) -> list[MessageRecord]:
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
        return [_message_record(r) for r in rows]

    async def get_message(self, conversation_id: uuid.UUID, message_id: int) -> MessageRecord | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id, Message.id == message_id)
                )
            ).scalar_one_or_none()
        return _message_record(row) if row else None

    async def _add_message(
        self,
        session: AsyncSession,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        sources: Sequence[dict[str, Any]],
    ) -> MessageRecord:
        row = Message(conversation_id=conversation_id, role=role.value, content=content, sources=list(sources))
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return _message_record(row)

    async def add_message(
        self, conversation_id: uuid.UUID, role: MessageRole, content: str, sources: Sequence[dict[str, Any]] = ()
    ) -> MessageRecord:
        async with self._sessionmaker.begin() as session:
            return await self._add_message(session, conversation_id, role, content, sources)

    async def set_message_rating(self, conversation_id: uuid.UUID, message_id: int, rating: Rating) -> MessageRecord:
        async with self._sessionmaker.begin() as session:
            row = (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id, Message.id == message_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ConversationNotFound(f"message {message_id}")
            row.rating = rating.value
            await session.flush()
            return _message_record(row)

    async def count_message_ratings(self, conversation_id: uuid.UUID, rating: Rating) -> int:
        async with self._sessionmaker() as session:
            count = (
                await session.execute(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == conversation_id, Message.rating == rating.value
                    )
                )
            ).scalar_one()
        return int(count)

    async def create_handoff_case(self, new: NewHandoffCase) -> HandoffResult:
        async with self._sessionmaker.begin() as session:
            conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.id == new.conversation_id).with_for_update()
                )
            ).scalar_one_or_none()
            if conversation is None:
                raise ConversationNotFound(str(new.conversation_id))
            if conversation.status != ConversationStatus.OPEN.value:
                raise ConversationClosed(str(new.conversation_id))
            case = Case(
                id=uuid.uuid4(),
                customer_id=new.customer_id,
                customer_name=new.customer_name or conversation.customer_name,
                conversation_id=new.conversation_id,
                source=CaseSource.ASSISTANT_HANDOFF.value,
                category=new.category.value,
                complaint=new.complaint,
                summary=new.summary,
                handoff_evidence=list(new.evidence),
                status=CaseStatus.NEW.value,
                simulated=conversation.simulated,
                # Answer keys travel from the conversation to the case for scoring only.
                expected_root_cause=conversation.expected_root_cause,
                expected_outcome=conversation.expected_outcome,
            )
            session.add(case)
            await session.flush()
            investigation_id: uuid.UUID | None = None
            if self._jobs is not None:
                investigation = await self._jobs.enqueue(session, case.id, trigger=InvestigationTrigger.AUTO)
                investigation_id = investigation.id
            else:
                log.warning("no job queue configured: case %s is not investigated automatically", case.id)
            notice = await self._add_message(session, new.conversation_id, MessageRole.ASSISTANT, new.notice, [])
            conversation.status = ConversationStatus.CLOSED.value
            conversation.outcome = ConversationOutcome.HANDED_OFF.value
            conversation.category = new.category.value
            conversation.ended_at = func.now()
            await session.flush()
            await notify(session, CASE_EVENTS_CHANNEL, case_created_payload(case.id))
            await session.refresh(conversation)
            record = _conversation_record(conversation, case.id)
        return HandoffResult(case.id, investigation_id, notice, record)
