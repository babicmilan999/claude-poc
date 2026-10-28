"""Customer-safe views of banking records.

What a customer-scoped tool returns. They keep what a customer may know (a card is
frozen, a payment failed for insufficient funds) and drop internal details: fraud rule
ids, risk scores, status reason codes, retry counts, raw reason codes, full account
numbers and sort codes. Reasons are customer-friendly phrases, not codes.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from core.banking.models import (
    Account,
    Balance,
    Card,
    CardAuthorisation,
    CardControls,
    CardStatus,
    CardStatusReason,
    CollectionFailureCode,
    DeclineReason,
    DirectDebitCollection,
    Mandate,
    PaymentAttempt,
    PaymentFailureReason,
    ScheduledPayment,
)

DECLINE_REASON_TEXT: dict[DeclineReason, str] = {
    DeclineReason.CARD_FROZEN: "card frozen",
    DeclineReason.CARD_BLOCKED: "card blocked",
    DeclineReason.CARD_EXPIRED: "card expired",
    DeclineReason.INSUFFICIENT_FUNDS: "insufficient funds",
    DeclineReason.CONTACTLESS_LIMIT_EXCEEDED: "over the contactless limit",
    DeclineReason.MERCHANT_CATEGORY_BLOCKED: "this type of shop is blocked in your card controls",
    DeclineReason.ONLINE_PAYMENTS_DISABLED: "online payments are turned off in your card controls",
    DeclineReason.FRAUD_RULE_TRIGGERED: "blocked for a security check",
    DeclineReason.ACCOUNT_NOT_ACTIVE: "account not active",
}

CARD_STATUS_TEXT: dict[CardStatusReason, str] = {
    CardStatusReason.FROZEN_BY_CUSTOMER: "frozen in the app",
    CardStatusReason.FRAUD_SUSPECTED: "blocked for a security check",
    CardStatusReason.LOST_OR_STOLEN: "reported lost or stolen",
    CardStatusReason.EXPIRED: "expired",
}

PAYMENT_FAILURE_TEXT: dict[PaymentFailureReason, str] = {
    PaymentFailureReason.INSUFFICIENT_FUNDS: "insufficient funds on the payment date",
    PaymentFailureReason.DAILY_PAYMENT_LIMIT_EXCEEDED: "daily payment limit reached",
    PaymentFailureReason.PAYEE_ACCOUNT_CLOSED: "the recipient's account is closed",
    PaymentFailureReason.INVALID_PAYEE_DETAILS: "the recipient's details are not valid",
    PaymentFailureReason.ACCOUNT_NOT_ACTIVE: "your account is not active",
}

COLLECTION_FAILURE_TEXT: dict[CollectionFailureCode, str] = {
    CollectionFailureCode.MANDATE_CANCELLED: "the Direct Debit instruction was cancelled",
    CollectionFailureCode.NO_MANDATE: "there is no Direct Debit instruction for this collection",
    CollectionFailureCode.INSUFFICIENT_FUNDS: "insufficient funds on the collection date",
    CollectionFailureCode.ACCOUNT_CLOSED_BY_ORIGINATOR: "the company closed the account this Direct Debit belongs to",
    CollectionFailureCode.PAYER_ACCOUNT_CLOSED: "your account is closed",
}


def mask_account_number(number: str) -> str:
    return f"****{number[-4:]}" if len(number) >= 4 else "****"


class CustomerView(BaseModel):
    model_config = ConfigDict(frozen=True)


class CustomerCard(CustomerView):
    id: str
    last4: str
    brand: str | None
    status: CardStatus
    status_detail: str | None
    expiry: date

    @classmethod
    def safe(cls, card: Card) -> "CustomerCard":
        detail = CARD_STATUS_TEXT.get(card.status_reason) if card.status_reason else None
        if card.status is CardStatus.BLOCKED and detail is None:
            detail = "blocked"
        return cls(
            id=card.id,
            last4=card.last4,
            brand=card.brand,
            status=card.status,
            status_detail=detail if card.status is not CardStatus.ACTIVE else None,
            expiry=card.expiry,
        )


class CustomerDecline(CustomerView):
    id: str
    card_id: str
    card_last4: str | None
    amount: Decimal
    currency: str
    merchant_name: str
    channel: str
    reason: str
    created_at: datetime

    @classmethod
    def safe(cls, auth: CardAuthorisation, card_last4: str | None) -> "CustomerDecline":
        reason = DECLINE_REASON_TEXT.get(auth.decline_reason, "declined") if auth.decline_reason else "declined"
        return cls(
            id=auth.id,
            card_id=auth.card_id,
            card_last4=card_last4,
            amount=auth.amount,
            currency=auth.currency,
            merchant_name=auth.merchant_name,
            channel=auth.channel,
            reason=reason,
            created_at=auth.created_at,
        )


class CustomerCardControls(CustomerView):
    card_id: str
    card_last4: str | None
    contactless_limit: Decimal
    blocked_merchant_categories: list[str]
    online_payments_enabled: bool

    @classmethod
    def safe(cls, controls: CardControls, card_last4: str | None) -> "CustomerCardControls":
        return cls(
            card_id=controls.card_id,
            card_last4=card_last4,
            contactless_limit=controls.contactless_limit,
            blocked_merchant_categories=list(controls.blocked_merchant_categories),
            online_payments_enabled=controls.online_payments_enabled,
        )


class CustomerPaymentAttempt(CustomerView):
    result: str
    reason: str | None
    attempted_at: datetime

    @classmethod
    def safe(cls, attempt: PaymentAttempt) -> "CustomerPaymentAttempt":
        reason = PAYMENT_FAILURE_TEXT.get(attempt.failure_reason) if attempt.failure_reason else None
        return cls(result=attempt.result, reason=reason, attempted_at=attempt.attempted_at)


class CustomerScheduledPayment(CustomerView):
    id: str
    payee_name: str | None
    reference: str | None
    amount: Decimal
    currency: str
    frequency: str
    next_run: date
    status: str
    last_attempt: CustomerPaymentAttempt | None

    @classmethod
    def safe(cls, payment: ScheduledPayment) -> "CustomerScheduledPayment":
        return cls(
            id=payment.id,
            payee_name=payment.payee_name,
            reference=payment.reference,
            amount=payment.amount,
            currency=payment.currency,
            frequency=payment.frequency,
            next_run=payment.next_run,
            status=payment.status,
            last_attempt=CustomerPaymentAttempt.safe(payment.last_attempt) if payment.last_attempt else None,
        )


class CustomerCollection(CustomerView):
    id: str
    amount: Decimal
    currency: str
    due_date: date
    result: str
    reason: str | None

    @classmethod
    def safe(cls, collection: DirectDebitCollection) -> "CustomerCollection":
        reason = COLLECTION_FAILURE_TEXT.get(collection.failure_code) if collection.failure_code else None
        return cls(
            id=collection.id,
            amount=collection.amount,
            currency=collection.currency,
            due_date=collection.due_date,
            result=collection.result,
            reason=reason,
        )


class CustomerDirectDebit(CustomerView):
    id: str
    originator: str
    reference: str
    status: str
    cancelled_by_you: bool
    cancelled_at: datetime | None
    recent_collections: list[CustomerCollection]

    @classmethod
    def safe(cls, mandate: Mandate, collections: list[DirectDebitCollection]) -> "CustomerDirectDebit":
        return cls(
            id=mandate.id,
            originator=mandate.originator,
            reference=mandate.reference,
            status=mandate.status,
            cancelled_by_you=mandate.cancelled_by == "customer",
            cancelled_at=mandate.cancelled_at,
            recent_collections=[CustomerCollection.safe(c) for c in collections],
        )


class CustomerBalance(CustomerView):
    account_id: str
    account_name: str | None
    masked_account_number: str
    account_status: str
    currency: str
    current: Decimal
    available: Decimal
    overdraft_limit: Decimal
    as_of: datetime

    @classmethod
    def safe(cls, account: Account, balance: Balance) -> "CustomerBalance":
        return cls(
            account_id=account.id,
            account_name=account.name,
            masked_account_number=mask_account_number(account.account_number),
            account_status=account.status,
            currency=balance.currency,
            current=balance.current,
            available=balance.available,
            overdraft_limit=balance.overdraft_limit,
            as_of=balance.as_of,
        )
