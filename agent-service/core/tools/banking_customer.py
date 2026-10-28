"""Customer-scoped banking tools (the assistant).

`make_customer_tools(banking, customer_id)` binds the logged-in customer's id from the
session into every tool. No tool declares a `customer_id` argument and every args schema
forbids unknown arguments, so the model cannot read another customer's data however it
is prompted. Results are customer-safe views (`customer_views`): no fraud flags, risk
scores, rule ids or internal codes. All tools are read-only.
"""

import asyncio
from collections import defaultdict
from datetime import timedelta

from langchain_core.tools import BaseTool
from pydantic import Field

from core.banking import BankingClient
from core.banking.models import AuthorisationResult
from core.tools.common import Clock, NoArgs, ToolArgs, make_tool, utc_now
from core.tools.customer_views import (
    CustomerBalance,
    CustomerCard,
    CustomerCardControls,
    CustomerDecline,
    CustomerDirectDebit,
    CustomerScheduledPayment,
)

CUSTOMER_TOOL_NAMES = (
    "get_my_cards",
    "get_my_recent_declines",
    "get_my_card_controls",
    "get_my_scheduled_payments",
    "get_my_direct_debits",
    "get_my_balance",
)


class RecentDeclinesArgs(ToolArgs):
    days: int = Field(default=30, ge=1, le=90, description="How many days back to look.")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of declines.")


class CardControlsArgs(ToolArgs):
    card_id: str | None = Field(
        default=None,
        description="Id of one of the customer's cards (from get_my_cards). Omit for all cards.",
    )


def make_customer_tools(banking: BankingClient, customer_id: str, *, clock: Clock = utc_now) -> list[BaseTool]:
    """Customer-scoped tools for one session. `customer_id` comes from the session only."""
    if not customer_id:
        raise ValueError("customer-scoped tools need the session's customer id")

    async def _card_last4() -> dict[str, str]:
        return {card.id: card.last4 for card in await banking.list_cards(customer_id)}

    async def get_my_cards() -> list[CustomerCard]:
        return [CustomerCard.safe(c) for c in await banking.list_cards(customer_id)]

    async def get_my_recent_declines(days: int = 30, limit: int = 10) -> list[CustomerDecline]:
        declines, last4 = await asyncio.gather(
            banking.list_authorisations(
                customer_id,
                result=AuthorisationResult.DECLINED,
                since=clock() - timedelta(days=days),
                limit=limit,
            ),
            _card_last4(),
        )
        return [CustomerDecline.safe(a, last4.get(a.card_id)) for a in declines]

    async def get_my_card_controls(card_id: str | None = None) -> list[CustomerCardControls]:
        last4 = await _card_last4()
        card_ids = [card_id] if card_id else list(last4)
        controls = await asyncio.gather(*(banking.get_card_controls(customer_id, cid) for cid in card_ids))
        return [CustomerCardControls.safe(c, last4.get(c.card_id)) for c in controls]

    async def get_my_scheduled_payments() -> list[CustomerScheduledPayment]:
        return [CustomerScheduledPayment.safe(p) for p in await banking.list_scheduled_payments(customer_id)]

    async def get_my_direct_debits() -> list[CustomerDirectDebit]:
        mandates, collections = await asyncio.gather(
            banking.list_mandates(customer_id),
            banking.list_direct_debit_collections(customer_id, limit=50),
        )
        by_mandate = defaultdict(list)
        for collection in collections:
            by_mandate[collection.mandate_id].append(collection)
        return [CustomerDirectDebit.safe(m, by_mandate[m.id][:3]) for m in mandates]

    async def get_my_balance() -> list[CustomerBalance]:
        accounts = await banking.list_accounts(customer_id)
        balances = await asyncio.gather(*(banking.get_balance(customer_id, a.id) for a in accounts))
        return [CustomerBalance.safe(a, b) for a, b in zip(accounts, balances, strict=True)]

    return [
        make_tool(
            name="get_my_cards",
            description="List the customer's cards with their status (active, frozen, blocked, expired) and expiry date.",
            args_schema=NoArgs,
            func=get_my_cards,
        ),
        make_tool(
            name="get_my_recent_declines",
            description=(
                "List the customer's recent declined card payments with merchant, amount and a "
                "customer-friendly reason (for example 'card frozen' or 'insufficient funds')."
            ),
            args_schema=RecentDeclinesArgs,
            func=get_my_recent_declines,
        ),
        make_tool(
            name="get_my_card_controls",
            description=(
                "Get the card controls the customer set: contactless limit, blocked merchant "
                "categories (ISO 18245 codes) and whether online payments are on."
            ),
            args_schema=CardControlsArgs,
            func=get_my_card_controls,
        ),
        make_tool(
            name="get_my_scheduled_payments",
            description="List the customer's standing orders and scheduled payments with their last attempt and its result.",
            args_schema=NoArgs,
            func=get_my_scheduled_payments,
        ),
        make_tool(
            name="get_my_direct_debits",
            description="List the customer's Direct Debit mandates with their status and recent collections.",
            args_schema=NoArgs,
            func=get_my_direct_debits,
        ),
        make_tool(
            name="get_my_balance",
            description="Get the current and available balance of each of the customer's accounts.",
            args_schema=NoArgs,
            func=get_my_balance,
        ),
    ]
