"""Ops-scoped banking tools (the ops copilot's investigators).

Full records, read-only. The case's customer id is bound when the tools are made (an
investigation is always about one case and one customer), so the model never has to
supply or guess it. Sensitive identifiers (account numbers, sort codes) are still masked
before results go back to the model.

`OPS_TOOLSETS` restricts each investigator to the tools for its issue type
(architecture section 6); `generic` gets the read-only union.
"""

import asyncio
from datetime import timedelta

from langchain_core.tools import BaseTool
from pydantic import Field

from core.banking import BankingClient
from core.banking.models import (
    AuthorisationResult,
    CollectionResult,
    FraudFlagStatus,
    MandateStatus,
    PaymentAttemptResult,
)
from core.enums import IssueType
from core.tools.common import Clock, NoArgs, ToolArgs, make_tool, utc_now

CARD_DECLINE_TOOLS = (
    "get_cards",
    "get_card_authorisations",
    "get_account_balance",
    "get_card_controls",
    "get_fraud_flags",
)
MISSED_PAYMENT_TOOLS = (
    "get_scheduled_payments",
    "get_payment_attempts",
    "get_account_balance",
    "get_transactions",
    "get_payee",
)
FAILED_DIRECT_DEBIT_TOOLS = (
    "get_mandates",
    "get_direct_debit_collections",
    "get_account_balance",
    "get_transactions",
)
ALL_OPS_TOOLS: tuple[str, ...] = tuple(
    dict.fromkeys(CARD_DECLINE_TOOLS + MISSED_PAYMENT_TOOLS + FAILED_DIRECT_DEBIT_TOOLS)
)

OPS_TOOLSETS: dict[IssueType, tuple[str, ...]] = {
    IssueType.CARD_DECLINE: CARD_DECLINE_TOOLS,
    IssueType.MISSED_PAYMENT: MISSED_PAYMENT_TOOLS,
    IssueType.FAILED_DIRECT_DEBIT: FAILED_DIRECT_DEBIT_TOOLS,
    IssueType.OTHER: ALL_OPS_TOOLS,
}


class _Window(ToolArgs):
    since_days: int = Field(default=30, ge=1, le=365, description="How many days back to look.")
    limit: int = Field(default=20, ge=1, le=200, description="Maximum number of items, newest first.")


class AuthorisationsArgs(_Window):
    card_id: str | None = Field(default=None, description="Only this card (id from get_cards).")
    result: AuthorisationResult | None = Field(default=None, description="approved or declined.")


class BalanceArgs(ToolArgs):
    account_id: str | None = Field(default=None, description="One account id; omit for every account.")


class CardIdArgs(ToolArgs):
    card_id: str = Field(description="Card id from get_cards.")


class FraudFlagsArgs(ToolArgs):
    card_id: str | None = Field(default=None, description="Only flags for this card.")
    status: FraudFlagStatus | None = Field(default=None, description="open, cleared or confirmed_fraud.")


class PaymentAttemptsArgs(_Window):
    scheduled_payment_id: str | None = Field(default=None, description="Only attempts of this scheduled payment.")
    result: list[PaymentAttemptResult] | None = Field(
        default=None, description="Only these results, for example ['failed', 'skipped']."
    )


class TransactionsArgs(_Window):
    account_id: str | None = Field(default=None, description="One account id; omit for every account.")


class PayeeArgs(ToolArgs):
    payee_id: str = Field(description="Payee id, for example from a scheduled payment.")


class MandatesArgs(ToolArgs):
    status: MandateStatus | None = Field(default=None, description="active, cancelled or dormant.")


class CollectionsArgs(_Window):
    mandate_id: str | None = Field(default=None, description="Only collections of this mandate.")
    result: CollectionResult | None = Field(default=None, description="pending, collected or failed.")


def make_ops_tools(banking: BankingClient, customer_id: str, *, clock: Clock = utc_now) -> list[BaseTool]:
    """Every ops-scoped tool, bound to the case's customer."""
    if not customer_id:
        raise ValueError("ops tools need the case's customer id")
    cid = customer_id

    async def _account_ids(account_id: str | None) -> list[str]:
        if account_id:
            return [account_id]
        return [a.id for a in await banking.list_accounts(cid)]

    async def get_cards() -> object:
        return await banking.list_cards(cid)

    async def get_card_authorisations(
        since_days: int = 30,
        limit: int = 20,
        card_id: str | None = None,
        result: AuthorisationResult | None = None,
    ) -> object:
        return await banking.list_authorisations(
            cid, card_id=card_id, result=result, since=clock() - timedelta(days=since_days), limit=limit
        )

    async def get_account_balance(account_id: str | None = None) -> object:
        accounts = await banking.list_accounts(cid)
        wanted_ids = [account_id] if account_id else [a.id for a in accounts]
        balances = await asyncio.gather(*(banking.get_balance(cid, a) for a in wanted_ids))
        status = {a.id: a.status for a in accounts}
        return [{**b.model_dump(mode="json"), "account_status": status.get(b.account_id)} for b in balances]

    async def get_card_controls(card_id: str) -> object:
        return await banking.get_card_controls(cid, card_id)

    async def get_fraud_flags(card_id: str | None = None, status: FraudFlagStatus | None = None) -> object:
        return await banking.list_fraud_flags(cid, card_id=card_id, status=status)

    async def get_scheduled_payments() -> object:
        return await banking.list_scheduled_payments(cid)

    async def get_payment_attempts(
        since_days: int = 30,
        limit: int = 20,
        scheduled_payment_id: str | None = None,
        result: list[PaymentAttemptResult] | None = None,
    ) -> object:
        return await banking.list_payment_attempts(
            cid,
            scheduled_payment_id=scheduled_payment_id,
            result=result,
            since=clock() - timedelta(days=since_days),
            limit=limit,
        )

    async def get_transactions(since_days: int = 30, limit: int = 20, account_id: str | None = None) -> object:
        since = clock() - timedelta(days=since_days)
        per_account = await asyncio.gather(
            *(banking.list_transactions(cid, a, since=since, limit=limit) for a in await _account_ids(account_id))
        )
        merged = [t for txs in per_account for t in txs]
        merged.sort(key=lambda t: t.booked_at, reverse=True)
        return merged[:limit]

    async def get_payee(payee_id: str) -> object:
        return await banking.get_payee(cid, payee_id)

    async def get_mandates(status: MandateStatus | None = None) -> object:
        return await banking.list_mandates(cid, status=status)

    async def get_direct_debit_collections(
        since_days: int = 30,
        limit: int = 20,
        mandate_id: str | None = None,
        result: CollectionResult | None = None,
    ) -> object:
        return await banking.list_direct_debit_collections(
            cid,
            mandate_id=mandate_id,
            result=result,
            since=clock() - timedelta(days=since_days),
            limit=limit,
        )

    return [
        make_tool(
            name="get_cards",
            description="List the customer's cards with status and internal status reason (for example FROZEN_BY_CUSTOMER, FRAUD_SUSPECTED).",
            args_schema=NoArgs,
            func=get_cards,
        ),
        make_tool(
            name="get_card_authorisations",
            description="List card authorisations (approved and declined) with decline reason codes, merchant category and fraud rule id, newest first.",
            args_schema=AuthorisationsArgs,
            func=get_card_authorisations,
        ),
        make_tool(
            name="get_account_balance",
            description="Get current and available balance, overdraft limit and account status for one or every account.",
            args_schema=BalanceArgs,
            func=get_account_balance,
        ),
        make_tool(
            name="get_card_controls",
            description="Get a card's controls: contactless limit, blocked merchant categories, online payments on or off.",
            args_schema=CardIdArgs,
            func=get_card_controls,
        ),
        make_tool(
            name="get_fraud_flags",
            description="List fraud flags on the customer (rule id, risk score, status, action taken). Internal data.",
            args_schema=FraudFlagsArgs,
            func=get_fraud_flags,
        ),
        make_tool(
            name="get_scheduled_payments",
            description="List standing orders and scheduled payments with payee, amount, next run date and last attempt.",
            args_schema=NoArgs,
            func=get_scheduled_payments,
        ),
        make_tool(
            name="get_payment_attempts",
            description="List execution attempts of scheduled payments with result, failure reason and retry count, newest first.",
            args_schema=PaymentAttemptsArgs,
            func=get_payment_attempts,
        ),
        make_tool(
            name="get_transactions",
            description="List booked transactions (money in and out) on one or every account, newest first.",
            args_schema=TransactionsArgs,
            func=get_transactions,
        ),
        make_tool(
            name="get_payee",
            description="Get a payee, including the status of the payee's account at the receiving bank.",
            args_schema=PayeeArgs,
            func=get_payee,
        ),
        make_tool(
            name="get_mandates",
            description="List Direct Debit mandates with originator, status and who cancelled them.",
            args_schema=MandatesArgs,
            func=get_mandates,
        ),
        make_tool(
            name="get_direct_debit_collections",
            description="List Direct Debit collections with result and failure code, newest first.",
            args_schema=CollectionsArgs,
            func=get_direct_debit_collections,
        ),
    ]


def ops_tools_for(
    banking: BankingClient, customer_id: str, issue_type: IssueType | str, *, clock: Clock = utc_now
) -> list[BaseTool]:
    """The restricted tool set of one investigator."""
    allowed = OPS_TOOLSETS[IssueType(issue_type)]
    by_name = {t.name: t for t in make_ops_tools(banking, customer_id, clock=clock)}
    return [by_name[name] for name in allowed]
