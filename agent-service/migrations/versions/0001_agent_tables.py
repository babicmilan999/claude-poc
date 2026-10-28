"""Agent tables (architecture section 9): conversations, messages, cases,
investigations (also the job queue), investigation events, diagnoses, reviews, and the
knowledge base chunks (pgvector).

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS agents")
    op.execute("CREATE SCHEMA IF NOT EXISTS kb")
    op.create_table(
        "conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("rating", sa.String(length=10), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expected_outcome", sa.String(length=20), nullable=True),
        sa.Column("expected_root_cause", sa.String(length=60), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IS NULL OR category IN ('card_decline', 'missed_payment', 'failed_direct_debit', 'other')",
            name=op.f("ck_conversations_category"),
        ),
        sa.CheckConstraint(
            "expected_outcome IS NULL OR expected_outcome IN ('self_serve', 'needs_human')",
            name=op.f("ck_conversations_expected_outcome"),
        ),
        sa.CheckConstraint(
            "expected_root_cause IS NULL OR expected_root_cause IN ('card_frozen_by_customer', 'insufficient_funds', 'card_expired', 'contactless_limit_exceeded', 'merchant_category_blocked', 'fraud_rule_triggered', 'daily_payment_limit_exceeded', 'payee_account_closed', 'mandate_cancelled_by_customer', 'account_closed_by_originator', 'online_payments_disabled', 'card_lost_or_stolen', 'account_not_active', 'invalid_payee_details', 'no_mandate', 'payer_account_closed', 'unknown')",
            name=op.f("ck_conversations_expected_root_cause"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('resolved', 'handed_off', 'abandoned')",
            name=op.f("ck_conversations_outcome"),
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating IN ('up', 'down')",
            name=op.f("ck_conversations_rating"),
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_conversations_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        schema="agents",
    )
    op.create_index(
        op.f("ix_conversations_customer_id_started_at"),
        "conversations",
        ["customer_id", "started_at"],
        unique=False,
        schema="agents",
    )
    op.create_index(
        op.f("ix_conversations_started_at"),
        "conversations",
        ["started_at"],
        unique=False,
        schema="agents",
    )
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("article_id", sa.String(length=200), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
        sa.UniqueConstraint(
            "article_id",
            "chunk_index",
            name=op.f("uq_kb_chunks_article_id_chunk_index"),
        ),
        schema="kb",
    )
    op.create_index(
        "ix_kb_chunks_embedding_hnsw",
        "kb_chunks",
        ["embedding"],
        unique=False,
        schema="kb",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_kb_chunks_tsv",
        "kb_chunks",
        ["tsv"],
        unique=False,
        schema="kb",
        postgresql_using="gin",
    )
    op.create_table(
        "cases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("complaint", sa.Text(), nullable=True),
        sa.Column("trigger_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "handoff_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expected_root_cause", sa.String(length=60), nullable=True),
        sa.Column("expected_outcome", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IS NULL OR category IN ('card_decline', 'missed_payment', 'failed_direct_debit', 'other')",
            name=op.f("ck_cases_category"),
        ),
        sa.CheckConstraint(
            "expected_outcome IS NULL OR expected_outcome IN ('self_serve', 'needs_human')",
            name=op.f("ck_cases_expected_outcome"),
        ),
        sa.CheckConstraint(
            "expected_root_cause IS NULL OR expected_root_cause IN ('card_frozen_by_customer', 'insufficient_funds', 'card_expired', 'contactless_limit_exceeded', 'merchant_category_blocked', 'fraud_rule_triggered', 'daily_payment_limit_exceeded', 'payee_account_closed', 'mandate_cancelled_by_customer', 'account_closed_by_originator', 'online_payments_disabled', 'card_lost_or_stolen', 'account_not_active', 'invalid_payee_details', 'no_mandate', 'payer_account_closed', 'unknown')",
            name=op.f("ck_cases_expected_root_cause"),
        ),
        sa.CheckConstraint(
            "source IN ('assistant_handoff', 'email', 'phone', 'ops_manual', 'simulated_email', 'event')",
            name=op.f("ck_cases_source"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'investigating', 'awaiting_review', 'needs_manual', 'escalated', 'resolved')",
            name=op.f("ck_cases_status"),
        ),
        sa.CheckConstraint(
            "complaint IS NOT NULL OR trigger_payload IS NOT NULL",
            name=op.f("ck_cases_complaint_or_payload"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["agents.conversations.id"],
            name=op.f("fk_cases_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        schema="agents",
    )
    op.create_index(
        op.f("ix_cases_conversation_id"),
        "cases",
        ["conversation_id"],
        unique=False,
        schema="agents",
    )
    op.create_index(
        op.f("ix_cases_customer_id"),
        "cases",
        ["customer_id"],
        unique=False,
        schema="agents",
    )
    op.create_index(
        op.f("ix_cases_status_created_at"),
        "cases",
        ["status", "created_at"],
        unique=False,
        schema="agents",
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("rating", sa.String(length=10), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating IN ('up', 'down')",
            name=op.f("ck_messages_rating"),
        ),
        sa.CheckConstraint("role IN ('customer', 'assistant', 'system')", name=op.f("ck_messages_role")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["agents.conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        schema="agents",
    )
    op.create_index(
        op.f("ix_messages_conversation_id_id"),
        "messages",
        ["conversation_id", "id"],
        unique=False,
        schema="agents",
    )
    op.create_table(
        "investigations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issue_type", sa.String(length=40), nullable=True),
        sa.Column("category_changed", sa.Boolean(), nullable=True),
        sa.Column("steps_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("escalation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("langfuse_trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "issue_type IS NULL OR issue_type IN ('card_decline', 'missed_payment', 'failed_direct_debit', 'other')",
            name=op.f("ck_investigations_issue_type"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_review', 'completed', 'failed', 'superseded')",
            name=op.f("ck_investigations_status"),
        ),
        sa.CheckConstraint("trigger IN ('auto', 'manual')", name=op.f("ck_investigations_trigger")),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["agents.cases.id"],
            name=op.f("fk_investigations_case_id_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
        schema="agents",
    )
    op.create_index(
        op.f("ix_investigations_case_id_queued_at"),
        "investigations",
        ["case_id", "queued_at"],
        unique=False,
        schema="agents",
    )
    op.create_index(
        "ix_investigations_queue",
        "investigations",
        ["queued_at"],
        unique=False,
        schema="agents",
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "uq_investigations_one_active_per_case",
        "investigations",
        ["case_id"],
        unique=True,
        schema="agents",
        postgresql_where=sa.text("status IN ('queued', 'running', 'awaiting_review')"),
    )
    op.create_table(
        "diagnoses",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("issue_type", sa.String(length=40), nullable=False),
        sa.Column("root_cause", sa.String(length=60), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "issue_type IN ('card_decline', 'missed_payment', 'failed_direct_debit', 'other')",
            name=op.f("ck_diagnoses_issue_type"),
        ),
        sa.CheckConstraint(
            "root_cause IN ('card_frozen_by_customer', 'insufficient_funds', 'card_expired', 'contactless_limit_exceeded', 'merchant_category_blocked', 'fraud_rule_triggered', 'daily_payment_limit_exceeded', 'payee_account_closed', 'mandate_cancelled_by_customer', 'account_closed_by_originator', 'online_payments_disabled', 'card_lost_or_stolen', 'account_not_active', 'invalid_payee_details', 'no_mandate', 'payer_account_closed', 'unknown')",
            name=op.f("ck_diagnoses_root_cause"),
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=op.f("ck_diagnoses_confidence")),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["agents.investigations.id"],
            name=op.f("fk_diagnoses_investigation_id_investigations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagnoses")),
        sa.UniqueConstraint("investigation_id", name=op.f("uq_diagnoses_investigation_id")),
        schema="agents",
    )
    op.create_table(
        "investigation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('step_started', 'classified', 'tool_call', 'evidence', 'token', 'diagnosis', 'escalated', 'awaiting_review', 'review_recorded', 'status_changed')",
            name=op.f("ck_investigation_events_type"),
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["agents.investigations.id"],
            name=op.f("fk_investigation_events_investigation_id_investigations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_events")),
        schema="agents",
    )
    op.create_index(
        op.f("ix_investigation_events_investigation_id_id"),
        "investigation_events",
        ["investigation_id", "id"],
        unique=False,
        schema="agents",
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("diagnosis_id", sa.UUID(), nullable=True),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("corrected_issue_type", sa.String(length=40), nullable=True),
        sa.Column("corrected_root_cause", sa.String(length=60), nullable=True),
        sa.Column("hint", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "corrected_issue_type IS NULL OR corrected_issue_type IN ('card_decline', 'missed_payment', 'failed_direct_debit', 'other')",
            name=op.f("ck_reviews_corrected_issue_type"),
        ),
        sa.CheckConstraint(
            "corrected_root_cause IS NULL OR corrected_root_cause IN ('card_frozen_by_customer', 'insufficient_funds', 'card_expired', 'contactless_limit_exceeded', 'merchant_category_blocked', 'fraud_rule_triggered', 'daily_payment_limit_exceeded', 'payee_account_closed', 'mandate_cancelled_by_customer', 'account_closed_by_originator', 'online_payments_disabled', 'card_lost_or_stolen', 'account_not_active', 'invalid_payee_details', 'no_mandate', 'payer_account_closed', 'unknown')",
            name=op.f("ck_reviews_corrected_root_cause"),
        ),
        sa.CheckConstraint(
            "verdict <> 'corrected' OR corrected_root_cause IS NOT NULL",
            name=op.f("ck_reviews_corrected_needs_root_cause"),
        ),
        sa.CheckConstraint(
            "verdict <> 'rejected' OR hint IS NOT NULL",
            name=op.f("ck_reviews_rejected_needs_hint"),
        ),
        sa.CheckConstraint(
            "verdict IN ('accepted', 'corrected', 'rejected')",
            name=op.f("ck_reviews_verdict"),
        ),
        sa.ForeignKeyConstraint(
            ["diagnosis_id"],
            ["agents.diagnoses.id"],
            name=op.f("fk_reviews_diagnosis_id_diagnoses"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["agents.investigations.id"],
            name=op.f("fk_reviews_investigation_id_investigations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint("investigation_id", name=op.f("uq_reviews_investigation_id")),
        schema="agents",
    )


def downgrade() -> None:
    op.drop_table("reviews", schema="agents")
    op.drop_index(
        op.f("ix_investigation_events_investigation_id_id"),
        table_name="investigation_events",
        schema="agents",
    )
    op.drop_table("investigation_events", schema="agents")
    op.drop_table("diagnoses", schema="agents")
    op.drop_index(
        "uq_investigations_one_active_per_case",
        table_name="investigations",
        schema="agents",
        postgresql_where=sa.text("status IN ('queued', 'running', 'awaiting_review')"),
    )
    op.drop_index(
        "ix_investigations_queue",
        table_name="investigations",
        schema="agents",
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.drop_index(
        op.f("ix_investigations_case_id_queued_at"),
        table_name="investigations",
        schema="agents",
    )
    op.drop_table("investigations", schema="agents")
    op.drop_index(op.f("ix_messages_conversation_id_id"), table_name="messages", schema="agents")
    op.drop_table("messages", schema="agents")
    op.drop_index(op.f("ix_cases_status_created_at"), table_name="cases", schema="agents")
    op.drop_index(op.f("ix_cases_customer_id"), table_name="cases", schema="agents")
    op.drop_index(op.f("ix_cases_conversation_id"), table_name="cases", schema="agents")
    op.drop_table("cases", schema="agents")
    op.drop_index("ix_kb_chunks_tsv", table_name="kb_chunks", schema="kb", postgresql_using="gin")
    op.drop_index(
        "ix_kb_chunks_embedding_hnsw",
        table_name="kb_chunks",
        schema="kb",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("kb_chunks", schema="kb")
    op.drop_index(op.f("ix_conversations_started_at"), table_name="conversations", schema="agents")
    op.drop_index(
        op.f("ix_conversations_customer_id_started_at"),
        table_name="conversations",
        schema="agents",
    )
    op.drop_table("conversations", schema="agents")
    op.execute("DROP SCHEMA IF EXISTS kb")
