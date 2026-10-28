"""Signal snapshot: what is happening on the account right now (architecture section 3).

A fixed set of plain banking API calls, no LLM: recent card declines with reason codes,
failed or skipped scheduled payments, failed direct debit collections, and account and
card status. It grounds triage (facts win over vague words) and gives the assistant facts
for its answer.

`SignalSnapshot` is the full (ops) view with internal codes. `customer_safe()` gives the
view the assistant may show a customer. `to_prompt()` renders either as compact text.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from core.banking import BankingClient, BankingError
from core.banking.models import (
    AccountStatus,
    AuthorisationResult,
    CardStatus,
    CollectionResult,
    PaymentAttemptResult,
)
from core.enums import IssueType
from core.tools.common import Clock, utc_now
from core.tools.customer_views import (
    CARD_STATUS_TEXT,
    COLLECTION_FAILURE_TEXT,
    DECLINE_REASON_TEXT,
    PAYMENT_FAILURE_TEXT,
    mask_account_number,
)

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
LOW_BALANCE_THRESHOLD = Decimal("20.00")


class AccountSignal(BaseModel):
    account_id: str
    name: str | None
    masked_account_number: str
    status: str
    currency: str
    balance: Decimal
    overdraft_limit: Decimal
    low_balance: bool


class CardSignal(BaseModel):
    card_id: str
    last4: str
    status: str
    status_reason: str | None  # internal code
    expiry: date


class DeclineSignal(BaseModel):
    authorisation_id: str
    card_id: str
    card_last4: str | None
    amount: Decimal
    currency: str
    merchant_name: str
    merchant_mcc: str
    channel: str
    decline_reason: str | None  # internal code
    at: datetime


class FailedPaymentSignal(BaseModel):
    attempt_id: str
    scheduled_payment_id: str
    payee_name: str | None
    amount: Decimal | None
    result: str
    failure_reason: str | None  # internal code
    at: datetime


class FailedCollectionSignal(BaseModel):
    collection_id: str
    mandate_id: str
    originator: str | None
    amount: Decimal
    currency: str
    failure_code: str | None  # internal code
    due_date: date


class SignalSnapshot(BaseModel):
    customer_id: str
    taken_at: datetime
    lookback_days: int
    customer_status: str | None = None
    customer_vulnerable: bool = False  # internal; a sensitive-topic handoff signal
    accounts: list[AccountSignal] = Field(default_factory=list)
    cards: list[CardSignal] = Field(default_factory=list)
    recent_declines: list[DeclineSignal] = Field(default_factory=list)
    failed_payments: list[FailedPaymentSignal] = Field(default_factory=list)
    failed_collections: list[FailedCollectionSignal] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)  # lookups that failed

    # --- derived -------------------------------------------------------------------

    def signal_categories(self) -> list[IssueType]:
        """Issue types with evidence on the account, most recent first."""
        latest: dict[IssueType, datetime] = {}
        for d in self.recent_declines:
            latest[IssueType.CARD_DECLINE] = max(latest.get(IssueType.CARD_DECLINE, d.at), d.at)
        for p in self.failed_payments:
            latest[IssueType.MISSED_PAYMENT] = max(latest.get(IssueType.MISSED_PAYMENT, p.at), p.at)
        for c in self.failed_collections:
            at = datetime.combine(c.due_date, datetime.min.time(), tzinfo=self.taken_at.tzinfo)
            latest[IssueType.FAILED_DIRECT_DEBIT] = max(latest.get(IssueType.FAILED_DIRECT_DEBIT, at), at)
        return [t for t, _ in sorted(latest.items(), key=lambda kv: kv[1], reverse=True)]

    def has_fraud_block(self) -> bool:
        return any(c.status_reason == "FRAUD_SUSPECTED" for c in self.cards) or any(
            d.decline_reason == "FRAUD_RULE_TRIGGERED" for d in self.recent_declines
        )

    @property
    def is_empty(self) -> bool:
        return not (self.recent_declines or self.failed_payments or self.failed_collections)

    # --- views -----------------------------------------------------------------------

    def customer_safe(self) -> dict[str, Any]:
        """What the assistant may tell the customer: friendly reasons, no internal codes."""
        return {
            "accounts": [
                {
                    "name": a.name,
                    "masked_account_number": a.masked_account_number,
                    "status": a.status,
                    "balance": str(a.balance),
                    "currency": a.currency,
                    "low_balance": a.low_balance,
                }
                for a in self.accounts
            ],
            "cards": [
                {
                    "last4": c.last4,
                    "status": c.status,
                    "status_detail": _friendly(CARD_STATUS_TEXT, c.status_reason)
                    if c.status != CardStatus.ACTIVE
                    else None,
                    "expiry": c.expiry.isoformat(),
                }
                for c in self.cards
            ],
            "recent_declines": [
                {
                    "card_last4": d.card_last4,
                    "merchant": d.merchant_name,
                    "amount": str(d.amount),
                    "currency": d.currency,
                    "reason": _friendly(DECLINE_REASON_TEXT, d.decline_reason) or "declined",
                    "at": d.at.isoformat(),
                }
                for d in self.recent_declines
            ],
            "failed_payments": [
                {
                    "payee": p.payee_name,
                    "amount": str(p.amount) if p.amount is not None else None,
                    "result": p.result,
                    "reason": _friendly(PAYMENT_FAILURE_TEXT, p.failure_reason),
                    "at": p.at.isoformat(),
                }
                for p in self.failed_payments
            ],
            "failed_direct_debits": [
                {
                    "originator": c.originator,
                    "amount": str(c.amount),
                    "currency": c.currency,
                    "reason": _friendly(COLLECTION_FAILURE_TEXT, c.failure_code),
                    "due_date": c.due_date.isoformat(),
                }
                for c in self.failed_collections
            ],
        }

    def to_prompt(self, *, audience: str = "ops") -> str:
        """Compact text for a prompt. `audience="customer"` uses the customer-safe view."""
        if audience == "customer":
            safe = self.customer_safe()
            lines = ["Account facts (customer-safe):"]
            for a in safe["accounts"]:
                lines.append(
                    f"- account {a['masked_account_number']}: {a['status']}, balance {a['balance']} {a['currency']}"
                    + (" (low)" if a["low_balance"] else "")
                )
            for c in safe["cards"]:
                lines.append(
                    f"- card ****{c['last4']}: {c['status']}"
                    + (f" ({c['status_detail']})" if c["status_detail"] else "")
                )
            for d in safe["recent_declines"]:
                lines.append(
                    f"- declined {d['amount']} {d['currency']} at {d['merchant']} on card ****{d['card_last4']}: {d['reason']} ({d['at']})"
                )
            for p in safe["failed_payments"]:
                lines.append(f"- scheduled payment to {p['payee']} {p['result']}: {p['reason']} ({p['at']})")
            for c in safe["failed_direct_debits"]:
                lines.append(f"- Direct Debit to {c['originator']} failed: {c['reason']} (due {c['due_date']})")
            if len(lines) == 1:
                lines.append("- nothing unusual")
            return "\n".join(lines)

        lines = [f"Signal snapshot (last {self.lookback_days} days, taken {self.taken_at.isoformat()}):"]
        lines.append(f"- customer status: {self.customer_status or 'unknown'}")
        for a in self.accounts:
            lines.append(
                f"- account {a.account_id} ({a.masked_account_number}): {a.status}, balance {a.balance} {a.currency}, "
                f"overdraft {a.overdraft_limit}" + (", LOW BALANCE" if a.low_balance else "")
            )
        for c in self.cards:
            lines.append(
                f"- card {c.card_id} ****{c.last4}: {c.status}"
                + (f" ({c.status_reason})" if c.status_reason else "")
                + f", expires {c.expiry.isoformat()}"
            )
        lines.append(f"- recent card declines: {len(self.recent_declines)}")
        for d in self.recent_declines:
            lines.append(
                f"  - {d.at.isoformat()} {d.amount} {d.currency} at {d.merchant_name} (MCC {d.merchant_mcc}, {d.channel}) "
                f"card ****{d.card_last4}: {d.decline_reason}"
            )
        lines.append(f"- failed or skipped scheduled payments: {len(self.failed_payments)}")
        for p in self.failed_payments:
            lines.append(f"  - {p.at.isoformat()} to {p.payee_name} {p.amount}: {p.result} {p.failure_reason}")
        lines.append(f"- failed direct debit collections: {len(self.failed_collections)}")
        for c in self.failed_collections:
            lines.append(f"  - due {c.due_date.isoformat()} {c.originator} {c.amount} {c.currency}: {c.failure_code}")
        if self.errors:
            lines.append(f"- lookups that failed: {', '.join(self.errors)}")
        return "\n".join(lines)


def _friendly(table: dict[Any, str], code: str | None) -> str | None:
    if code is None:
        return None
    for key, value in table.items():
        if str(key) == code:
            return value
    return None


async def build_signal_snapshot(
    banking: BankingClient,
    customer_id: str,
    *,
    lookback_days: int = LOOKBACK_DAYS,
    clock: Clock = utc_now,
) -> SignalSnapshot:
    """Run the fixed set of lookups concurrently. A failed lookup is recorded in
    `errors`; the rest of the snapshot is still returned."""
    now = clock()
    since = now - timedelta(days=lookback_days)

    calls = {
        "customer": banking.get_customer(customer_id),
        "accounts": banking.list_accounts(customer_id),
        "cards": banking.list_cards(customer_id),
        "declines": banking.list_authorisations(
            customer_id, result=AuthorisationResult.DECLINED, since=since, limit=20
        ),
        "payments": banking.list_payment_attempts(
            customer_id,
            result=[PaymentAttemptResult.FAILED, PaymentAttemptResult.SKIPPED],
            since=since,
            limit=20,
        ),
        "scheduled": banking.list_scheduled_payments(customer_id),
        "collections": banking.list_direct_debit_collections(
            customer_id, result=CollectionResult.FAILED, since=since, limit=20
        ),
    }
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    data: dict[str, Any] = {}
    errors: list[str] = []
    for name, result in zip(calls, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, BankingError):
                log.exception("signal snapshot lookup %s failed", name, exc_info=result)
            errors.append(name)
            data[name] = None
        else:
            data[name] = result

    snapshot = SignalSnapshot(customer_id=customer_id, taken_at=now, lookback_days=lookback_days, errors=errors)
    if data["customer"] is not None:
        snapshot.customer_status = data["customer"].status
        snapshot.customer_vulnerable = data["customer"].vulnerable
    for a in data["accounts"] or []:
        snapshot.accounts.append(
            AccountSignal(
                account_id=a.id,
                name=a.name,
                masked_account_number=mask_account_number(a.account_number),
                status=a.status,
                currency=a.currency,
                balance=a.balance,
                overdraft_limit=a.overdraft_limit,
                low_balance=a.status == AccountStatus.ACTIVE and a.balance + a.overdraft_limit < LOW_BALANCE_THRESHOLD,
            )
        )
    last4: dict[str, str] = {}
    for c in data["cards"] or []:
        last4[c.id] = c.last4
        snapshot.cards.append(
            CardSignal(card_id=c.id, last4=c.last4, status=c.status, status_reason=c.status_reason, expiry=c.expiry)
        )
    for d in data["declines"] or []:
        snapshot.recent_declines.append(
            DeclineSignal(
                authorisation_id=d.id,
                card_id=d.card_id,
                card_last4=last4.get(d.card_id),
                amount=d.amount,
                currency=d.currency,
                merchant_name=d.merchant_name,
                merchant_mcc=d.merchant_mcc,
                channel=d.channel,
                decline_reason=d.decline_reason,
                at=d.created_at,
            )
        )
    payee_names = {p.id: p.payee_name for p in data["scheduled"] or []}
    for p in data["payments"] or []:
        if p.result == PaymentAttemptResult.SUCCEEDED:
            continue
        snapshot.failed_payments.append(
            FailedPaymentSignal(
                attempt_id=p.id,
                scheduled_payment_id=p.scheduled_payment_id,
                payee_name=payee_names.get(p.scheduled_payment_id),
                amount=p.amount,
                result=p.result,
                failure_reason=p.failure_reason,
                at=p.attempted_at,
            )
        )
    for c in data["collections"] or []:
        if c.result != CollectionResult.FAILED:
            continue
        snapshot.failed_collections.append(
            FailedCollectionSignal(
                collection_id=c.id,
                mandate_id=c.mandate_id,
                originator=c.originator,
                amount=c.amount,
                currency=c.currency,
                failure_code=c.failure_code,
                due_date=c.due_date,
            )
        )
    return snapshot
