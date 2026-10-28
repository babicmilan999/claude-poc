"""Pydantic models for `contracts/banking-api.yaml` (full, ops-scoped records).

Field names, required fields and enums follow the contract; `tests/test_contracts.py`
checks them against the spec. Money is a `Decimal` (the contract sends a two-place
decimal string). Unknown fields are ignored, so additive contract changes do not break
the client.

The simulation endpoint (`POST /simulation/incidents`) is deliberately not modelled
here: its response carries the answer key, and this module is used by the agents' tools.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class BankingModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- enums -------------------------------------------------------------------


class CustomerStatus(StrEnum):
    ACTIVE = "active"
    RESTRICTED = "restricted"
    CLOSED = "closed"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class TransactionType(StrEnum):
    CARD_PAYMENT = "card_payment"
    SCHEDULED_PAYMENT = "scheduled_payment"
    DIRECT_DEBIT = "direct_debit"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    FEE = "fee"
    REFUND = "refund"


class CardStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class CardStatusReason(StrEnum):
    FROZEN_BY_CUSTOMER = "FROZEN_BY_CUSTOMER"
    FRAUD_SUSPECTED = "FRAUD_SUSPECTED"
    LOST_OR_STOLEN = "LOST_OR_STOLEN"
    EXPIRED = "EXPIRED"


class AuthorisationResult(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"


class DeclineReason(StrEnum):
    CARD_FROZEN = "CARD_FROZEN"
    CARD_BLOCKED = "CARD_BLOCKED"
    CARD_EXPIRED = "CARD_EXPIRED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    CONTACTLESS_LIMIT_EXCEEDED = "CONTACTLESS_LIMIT_EXCEEDED"
    MERCHANT_CATEGORY_BLOCKED = "MERCHANT_CATEGORY_BLOCKED"
    ONLINE_PAYMENTS_DISABLED = "ONLINE_PAYMENTS_DISABLED"
    FRAUD_RULE_TRIGGERED = "FRAUD_RULE_TRIGGERED"
    ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"


class FraudFlagStatus(StrEnum):
    OPEN = "open"
    CLEARED = "cleared"
    CONFIRMED_FRAUD = "confirmed_fraud"


class PaymentAttemptResult(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class PaymentFailureReason(StrEnum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    DAILY_PAYMENT_LIMIT_EXCEEDED = "DAILY_PAYMENT_LIMIT_EXCEEDED"
    PAYEE_ACCOUNT_CLOSED = "PAYEE_ACCOUNT_CLOSED"
    INVALID_PAYEE_DETAILS = "INVALID_PAYEE_DETAILS"
    ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"


class MandateStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    DORMANT = "dormant"


class CollectionResult(StrEnum):
    PENDING = "pending"
    COLLECTED = "collected"
    FAILED = "failed"


class CollectionFailureCode(StrEnum):
    MANDATE_CANCELLED = "MANDATE_CANCELLED"
    NO_MANDATE = "NO_MANDATE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ACCOUNT_CLOSED_BY_ORIGINATOR = "ACCOUNT_CLOSED_BY_ORIGINATOR"
    PAYER_ACCOUNT_CLOSED = "PAYER_ACCOUNT_CLOSED"


# --- records -----------------------------------------------------------------


class Customer(BankingModel):
    id: str
    name: str
    email: str | None = None
    status: CustomerStatus
    vulnerable: bool = False  # internal
    created_at: datetime


class Account(BankingModel):
    id: str
    customer_id: str
    name: str | None = None
    sort_code: str  # sensitive
    account_number: str  # sensitive
    currency: str
    balance: Decimal
    overdraft_limit: Decimal
    status: AccountStatus


class Balance(BankingModel):
    account_id: str
    currency: str
    current: Decimal
    available: Decimal
    overdraft_limit: Decimal
    as_of: datetime


class Transaction(BankingModel):
    id: str
    account_id: str
    amount: Decimal
    currency: str
    type: TransactionType
    description: str
    reference_id: str | None = None
    booked_at: datetime


class Card(BankingModel):
    id: str
    account_id: str
    last4: str
    brand: Literal["visa", "mastercard"] | None = None
    status: CardStatus
    status_reason: CardStatusReason | None = None  # internal
    expiry: date


class CardControls(BankingModel):
    card_id: str
    contactless_limit: Decimal
    blocked_merchant_categories: list[str]
    online_payments_enabled: bool


class CardAuthorisation(BankingModel):
    id: str
    card_id: str
    amount: Decimal
    currency: str
    merchant_name: str
    merchant_mcc: str
    channel: Literal["chip_and_pin", "contactless", "online"]
    result: AuthorisationResult
    decline_reason: DeclineReason | None = None
    fraud_rule_id: str | None = None  # internal
    created_at: datetime


class FraudFlag(BankingModel):
    id: str
    customer_id: str
    card_id: str | None = None
    rule_id: str
    description: str | None = None
    risk_score: float | None = None
    status: FraudFlagStatus
    action: Literal["card_blocked", "review_only"] | None = None
    created_at: datetime
    cleared_at: datetime | None = None


class PaymentAttempt(BankingModel):
    id: str
    scheduled_payment_id: str
    amount: Decimal | None = None
    result: PaymentAttemptResult
    failure_reason: PaymentFailureReason | None = None
    retry_count: int | None = None  # internal
    attempted_at: datetime


class ScheduledPayment(BankingModel):
    id: str
    account_id: str
    payee_id: str
    payee_name: str | None = None
    reference: str | None = None
    amount: Decimal
    currency: str
    frequency: Literal["once", "weekly", "monthly"]
    next_run: date
    status: Literal["active", "paused", "cancelled", "completed"]
    last_attempt: PaymentAttempt | None = None


class Payee(BankingModel):
    id: str
    customer_id: str
    name: str
    sort_code: str  # sensitive
    account_number: str  # sensitive
    account_status: Literal["open", "closed", "unknown"]


class Mandate(BankingModel):
    id: str
    account_id: str
    originator: str
    reference: str
    status: MandateStatus
    cancelled_by: Literal["customer", "originator", "bank"] | None = None
    created_at: datetime
    cancelled_at: datetime | None = None


class DirectDebitCollection(BankingModel):
    id: str
    mandate_id: str
    originator: str | None = None
    amount: Decimal
    currency: str
    due_date: date
    result: CollectionResult
    failure_code: CollectionFailureCode | None = None
    processed_at: datetime | None = None
