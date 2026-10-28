"""SQLAlchemy models for the agent tables (architecture section 9, shapes from
`contracts/agent-api.yaml`).

Schemas: `agents` (conversations, cases, investigations, events, diagnoses, reviews; the
`queued` investigation rows are the job queue) and `kb` (knowledge base chunks). The
LangGraph checkpointer manages its own tables.

`expected_root_cause` / `expected_outcome` are stored for scoring only. No agent, tool,
prompt or API response may read them.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.constants import EMBEDDING_DIM
from core.enums import (
    ACTIVE_INVESTIGATION_STATUSES,
    CaseSource,
    CaseStatus,
    ConversationOutcome,
    ConversationStatus,
    ExpectedOutcome,
    InvestigationEventType,
    InvestigationStatus,
    InvestigationTrigger,
    IssueType,
    MessageRole,
    Rating,
    RootCause,
    Verdict,
)

AGENTS_SCHEMA = "agents"
KB_SCHEMA = "kb"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _in(column: str, values: type[StrEnum] | tuple[StrEnum, ...], nullable: bool = False) -> str:
    items = ", ".join(f"'{v.value}'" for v in values)
    clause = f"{column} IN ({items})"
    return f"{column} IS NULL OR {clause}" if nullable else clause


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_in("status", ConversationStatus), name="status"),
        CheckConstraint(_in("outcome", ConversationOutcome, nullable=True), name="outcome"),
        CheckConstraint(_in("category", IssueType, nullable=True), name="category"),
        CheckConstraint(_in("rating", Rating, nullable=True), name="rating"),
        CheckConstraint(_in("expected_outcome", ExpectedOutcome, nullable=True), name="expected_outcome"),
        CheckConstraint(_in("expected_root_cause", RootCause, nullable=True), name="expected_root_cause"),
        Index(None, "customer_id", "started_at"),
        Index(None, "started_at"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ConversationStatus.OPEN.value)
    category: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str | None] = mapped_column(String(20))
    rating: Mapped[str | None] = mapped_column(String(10))  # latest 👍 / 👎
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Answer key, scoring only. Never shown to an agent or returned by the API.
    expected_outcome: Mapped[str | None] = mapped_column(String(20))
    expected_root_cause: Mapped[str | None] = mapped_column(String(60))
    started_at: Mapped[datetime] = _now()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_in("role", MessageRole), name="role"),
        CheckConstraint(_in("rating", Rating, nullable=True), name="rating"),
        Index(None, "conversation_id", "id"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    rating: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = _now()

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint(_in("source", CaseSource), name="source"),
        CheckConstraint(_in("status", CaseStatus), name="status"),
        CheckConstraint(_in("category", IssueType, nullable=True), name="category"),
        CheckConstraint("complaint IS NOT NULL OR trigger_payload IS NOT NULL", name="complaint_or_payload"),
        CheckConstraint(_in("expected_outcome", ExpectedOutcome, nullable=True), name="expected_outcome"),
        CheckConstraint(_in("expected_root_cause", RootCause, nullable=True), name="expected_root_cause"),
        Index(None, "status", "created_at"),
        Index(None, "customer_id"),
        Index(None, "conversation_id"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.conversations.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40))
    complaint: Mapped[str | None] = mapped_column(Text)
    trigger_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)  # handoff summary
    handoff_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CaseStatus.NEW.value)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Answer key, scoring only. Never shown to an agent, the ops console or the API.
    expected_root_cause: Mapped[str | None] = mapped_column(String(60))
    expected_outcome: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    investigations: Mapped[list["Investigation"]] = relationship(
        back_populates="case", order_by="Investigation.queued_at.desc()"
    )


class Investigation(Base):
    """One run of the ops copilot on a case. `queued` rows are the job queue."""

    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint(_in("status", InvestigationStatus), name="status"),
        CheckConstraint(_in("trigger", InvestigationTrigger), name="trigger"),
        CheckConstraint(_in("issue_type", IssueType, nullable=True), name="issue_type"),
        # Job queue: oldest queued first.
        Index("ix_investigations_queue", "queued_at", postgresql_where=text("status = 'queued'")),
        # At most one active investigation per case.
        Index(
            "uq_investigations_one_active_per_case",
            "case_id",
            unique=True,
            postgresql_where=text(
                "status IN (" + ", ".join(f"'{s.value}'" for s in ACTIVE_INVESTIGATION_STATUSES) + ")"
            ),
        ),
        Index(None, "case_id", "queued_at"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.cases.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(10), nullable=False, default=InvestigationTrigger.AUTO.value)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=InvestigationStatus.QUEUED.value)
    issue_type: Mapped[str | None] = mapped_column(String(40))
    category_changed: Mapped[bool | None] = mapped_column(Boolean)
    steps_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    escalation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # Job queue bookkeeping
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    worker_id: Mapped[str | None] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # e.g. a review to resume with
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))
    queued_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[Case] = relationship(back_populates="investigations")
    diagnosis: Mapped["Diagnosis | None"] = relationship(back_populates="investigation", uselist=False)
    review: Mapped["Review | None"] = relationship(back_populates="investigation", uselist=False)


class InvestigationEvent(Base):
    """Timeline events, stored with an increasing id (the SSE `id:`)."""

    __tablename__ = "investigation_events"
    __table_args__ = (
        CheckConstraint(_in("type", InvestigationEventType), name="type"),
        Index(None, "investigation_id", "id"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.investigations.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _now()


class Diagnosis(Base):
    """The current diagnosis of an investigation (one per investigation; earlier ones
    after a reject survive only in the event log)."""

    __tablename__ = "diagnoses"
    __table_args__ = (
        UniqueConstraint("investigation_id"),
        CheckConstraint(_in("issue_type", IssueType), name="issue_type"),
        CheckConstraint(_in("root_cause", RootCause), name="root_cause"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.investigations.id", ondelete="CASCADE"), nullable=False
    )
    issue_type: Mapped[str] = mapped_column(String(40), nullable=False)
    root_cause: Mapped[str] = mapped_column(String(60), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()

    investigation: Mapped[Investigation] = relationship(back_populates="diagnosis")


class Review(Base):
    """The ops agent's verdict. Linked to the investigation (escalations have no
    diagnosis) and to the diagnosis when there is one."""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("investigation_id"),
        CheckConstraint(_in("verdict", Verdict), name="verdict"),
        CheckConstraint(_in("corrected_issue_type", IssueType, nullable=True), name="corrected_issue_type"),
        CheckConstraint(_in("corrected_root_cause", RootCause, nullable=True), name="corrected_root_cause"),
        CheckConstraint(
            "verdict <> 'corrected' OR corrected_root_cause IS NOT NULL", name="corrected_needs_root_cause"
        ),
        CheckConstraint("verdict <> 'rejected' OR hint IS NOT NULL", name="rejected_needs_hint"),
        {"schema": AGENTS_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.investigations.id", ondelete="CASCADE"), nullable=False
    )
    diagnosis_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{AGENTS_SCHEMA}.diagnoses.id", ondelete="SET NULL")
    )
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    corrected_issue_type: Mapped[str | None] = mapped_column(String(40))
    corrected_root_cause: Mapped[str | None] = mapped_column(String(60))
    hint: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()

    investigation: Mapped[Investigation] = relationship(back_populates="review")


class KbChunk(Base):
    """Knowledge base chunks for hybrid search (pgvector + full-text)."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("article_id", "chunk_index"),
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_kb_chunks_tsv", "tsv", postgresql_using="gin"),
        {"schema": KB_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    article_id: Mapped[str] = mapped_column(String(200), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))
    created_at: Mapped[datetime] = _now()
