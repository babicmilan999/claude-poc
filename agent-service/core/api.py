"""Routes owned by the shared core: `GET /health`, `GET /customers`, `GET /me/overview`
(`contracts/agent-api.yaml`, tags `platform`, `shared-customers`, `customer-account`)."""

import asyncio
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.banking import BankingError, BankingNotFound
from core.context import Context, KnownCustomerId
from core.tools.customer_views import mask_account_number

router = APIRouter()

RECENT_TRANSACTIONS = 10


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    llm_profile: str


class CustomerSummary(BaseModel):
    id: str
    name: str
    simulated: bool = False


class OverviewAccount(BaseModel):
    id: str
    name: str
    masked_account_number: str
    currency: str
    balance: str
    status: str


class OverviewCard(BaseModel):
    id: str
    last4: str
    status: str
    expiry: str


class OverviewTransaction(BaseModel):
    id: str
    amount: str
    currency: str
    description: str
    booked_at: str


class CustomerOverview(BaseModel):
    customer: CustomerSummary
    accounts: list[OverviewAccount]
    cards: list[OverviewCard]
    recent_transactions: list[OverviewTransaction]


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


@router.get("/health", response_model=Health, tags=["platform"])
async def health(ctx: Context) -> Health:
    return Health(llm_profile=ctx.settings.llm_profile)


@router.get("/customers", response_model=list[CustomerSummary], tags=["shared-customers"])
async def list_customers(ctx: Context) -> list[CustomerSummary]:
    try:
        customers = await ctx.banking.list_customers()
    except BankingError as exc:
        raise HTTPException(status_code=502, detail="banking service unavailable") from exc
    return [CustomerSummary(id=c.id, name=c.name) for c in customers]


@router.get("/me/overview", response_model=CustomerOverview, tags=["customer-account"])
async def my_overview(ctx: Context, customer_id: KnownCustomerId) -> CustomerOverview:
    banking = ctx.banking
    try:
        customer, accounts, cards = await asyncio.gather(
            banking.get_customer(customer_id),
            banking.list_accounts(customer_id),
            banking.list_cards(customer_id),
        )
        per_account = await asyncio.gather(
            *(banking.list_transactions(customer_id, a.id, limit=RECENT_TRANSACTIONS) for a in accounts)
        )
    except BankingNotFound as exc:
        # Gone between the session check and these calls: treat as not logged in.
        raise HTTPException(status_code=401, detail="unknown customer") from exc
    except BankingError as exc:
        raise HTTPException(status_code=502, detail="banking service unavailable") from exc

    transactions = sorted((t for txs in per_account for t in txs), key=lambda t: t.booked_at, reverse=True)
    return CustomerOverview(
        customer=CustomerSummary(id=customer.id, name=customer.name),
        accounts=[
            OverviewAccount(
                id=a.id,
                name=a.name or "Account",
                masked_account_number=mask_account_number(a.account_number),
                currency=a.currency,
                balance=_money(a.balance),
                status=a.status,
            )
            for a in accounts
        ],
        cards=[OverviewCard(id=c.id, last4=c.last4, status=c.status, expiry=c.expiry.isoformat()) for c in cards],
        recent_transactions=[
            OverviewTransaction(
                id=t.id,
                amount=_money(t.amount),
                currency=t.currency,
                description=t.description,
                booked_at=t.booked_at.isoformat(),
            )
            for t in transactions[:RECENT_TRANSACTIONS]
        ],
    )
