"""Shared enums.

The values mirror `contracts/scenario.schema.json` (IssueType, RootCause, ExpectedOutcome)
and `contracts/agent-api.yaml` (statuses, sources). `tests/test_contracts.py` fails when they
drift apart.
"""

from enum import StrEnum


class IssueType(StrEnum):
    CARD_DECLINE = "card_decline"
    MISSED_PAYMENT = "missed_payment"
    FAILED_DIRECT_DEBIT = "failed_direct_debit"
    OTHER = "other"


class RootCause(StrEnum):
    CARD_FROZEN_BY_CUSTOMER = "card_frozen_by_customer"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    CONTACTLESS_LIMIT_EXCEEDED = "contactless_limit_exceeded"
    MERCHANT_CATEGORY_BLOCKED = "merchant_category_blocked"
    FRAUD_RULE_TRIGGERED = "fraud_rule_triggered"
    DAILY_PAYMENT_LIMIT_EXCEEDED = "daily_payment_limit_exceeded"
    PAYEE_ACCOUNT_CLOSED = "payee_account_closed"
    MANDATE_CANCELLED_BY_CUSTOMER = "mandate_cancelled_by_customer"
    ACCOUNT_CLOSED_BY_ORIGINATOR = "account_closed_by_originator"
    ONLINE_PAYMENTS_DISABLED = "online_payments_disabled"
    CARD_LOST_OR_STOLEN = "card_lost_or_stolen"
    ACCOUNT_NOT_ACTIVE = "account_not_active"
    INVALID_PAYEE_DETAILS = "invalid_payee_details"
    NO_MANDATE = "no_mandate"
    PAYER_ACCOUNT_CLOSED = "payer_account_closed"
    UNKNOWN = "unknown"


class ExpectedOutcome(StrEnum):
    SELF_SERVE = "self_serve"
    NEEDS_HUMAN = "needs_human"


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ConversationOutcome(StrEnum):
    RESOLVED = "resolved"
    HANDED_OFF = "handed_off"
    ABANDONED = "abandoned"


class Rating(StrEnum):
    UP = "up"
    DOWN = "down"


class MessageRole(StrEnum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class CaseSource(StrEnum):
    ASSISTANT_HANDOFF = "assistant_handoff"
    EMAIL = "email"
    PHONE = "phone"
    OPS_MANUAL = "ops_manual"
    SIMULATED_EMAIL = "simulated_email"
    EVENT = "event"


class CaseStatus(StrEnum):
    NEW = "new"
    INVESTIGATING = "investigating"
    AWAITING_REVIEW = "awaiting_review"
    NEEDS_MANUAL = "needs_manual"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class InvestigationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


# At most one investigation per case may be in one of these states.
ACTIVE_INVESTIGATION_STATUSES: tuple[InvestigationStatus, ...] = (
    InvestigationStatus.QUEUED,
    InvestigationStatus.RUNNING,
    InvestigationStatus.AWAITING_REVIEW,
)


class InvestigationTrigger(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class InvestigationEventType(StrEnum):
    STEP_STARTED = "step_started"
    CLASSIFIED = "classified"
    TOOL_CALL = "tool_call"
    EVIDENCE = "evidence"
    TOKEN = "token"
    DIAGNOSIS = "diagnosis"
    ESCALATED = "escalated"
    AWAITING_REVIEW = "awaiting_review"
    REVIEW_RECORDED = "review_recorded"
    STATUS_CHANGED = "status_changed"


class EvidenceOrigin(StrEnum):
    ASSISTANT = "assistant"
    INVESTIGATION = "investigation"
