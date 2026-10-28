"""Async client for the banking service's read-only API (`contracts/banking-api.yaml`).

Only GET requests: this client is what the agents' tools and the signal snapshot use.
"""

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter

from core.banking.models import (
    Account,
    AuthorisationResult,
    Balance,
    Card,
    CardAuthorisation,
    CardControls,
    CollectionResult,
    Customer,
    DirectDebitCollection,
    FraudFlag,
    FraudFlagStatus,
    Mandate,
    MandateStatus,
    Payee,
    PaymentAttempt,
    PaymentAttemptResult,
    ScheduledPayment,
    Transaction,
)

T = TypeVar("T", bound=BaseModel)


class BankingError(Exception):
    """The banking service failed or returned something unexpected."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BankingNotFound(BankingError):
    """404: the record does not exist or is not owned by this customer."""


def _params(**values: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, list | tuple):
            if value:
                out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = str(value)
    return out


class BankingClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "BankingClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # --- plumbing ------------------------------------------------------------

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise BankingError(f"banking service unreachable: {exc.__class__.__name__}") from exc
        if response.status_code == 404:
            raise BankingNotFound(f"not found: {path}", 404)
        if response.status_code >= 400:
            raise BankingError(
                f"banking service returned {response.status_code} for {path}",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise BankingError(f"invalid JSON from banking service for {path}") from exc

    async def _get_one(self, model: type[T], path: str, params: Mapping[str, str] | None = None) -> T:
        return model.model_validate(await self._get(path, params))

    async def _get_list(self, model: type[T], path: str, params: Mapping[str, str] | None = None) -> list[T]:
        return TypeAdapter(list[model]).validate_python(await self._get(path, params))  # type: ignore[valid-type]

    @staticmethod
    def _c(customer_id: str) -> str:
        return f"/customers/{_seg(customer_id)}"

    # --- customers & accounts --------------------------------------------------

    async def list_customers(self) -> list[Customer]:
        return await self._get_list(Customer, "/customers")

    async def get_customer(self, customer_id: str) -> Customer:
        return await self._get_one(Customer, self._c(customer_id))

    async def list_accounts(self, customer_id: str) -> list[Account]:
        return await self._get_list(Account, f"{self._c(customer_id)}/accounts")

    async def get_balance(self, customer_id: str, account_id: str) -> Balance:
        return await self._get_one(Balance, f"{self._c(customer_id)}/accounts/{_seg(account_id)}/balance")

    async def list_transactions(
        self,
        customer_id: str,
        account_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        return await self._get_list(
            Transaction,
            f"{self._c(customer_id)}/accounts/{_seg(account_id)}/transactions",
            _params(since=since, until=until, limit=limit),
        )

    # --- cards -------------------------------------------------------------------

    async def list_cards(self, customer_id: str) -> list[Card]:
        return await self._get_list(Card, f"{self._c(customer_id)}/cards")

    async def get_card_controls(self, customer_id: str, card_id: str) -> CardControls:
        return await self._get_one(CardControls, f"{self._c(customer_id)}/cards/{_seg(card_id)}/controls")

    async def list_authorisations(
        self,
        customer_id: str,
        *,
        card_id: str | None = None,
        result: AuthorisationResult | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[CardAuthorisation]:
        return await self._get_list(
            CardAuthorisation,
            f"{self._c(customer_id)}/authorisations",
            _params(card_id=card_id, result=result, since=since, limit=limit),
        )

    async def list_fraud_flags(
        self,
        customer_id: str,
        *,
        card_id: str | None = None,
        status: FraudFlagStatus | None = None,
    ) -> list[FraudFlag]:
        return await self._get_list(
            FraudFlag,
            f"{self._c(customer_id)}/fraud-flags",
            _params(card_id=card_id, status=status),
        )

    # --- payments ------------------------------------------------------------------

    async def list_scheduled_payments(self, customer_id: str) -> list[ScheduledPayment]:
        return await self._get_list(ScheduledPayment, f"{self._c(customer_id)}/scheduled-payments")

    async def list_payment_attempts(
        self,
        customer_id: str,
        *,
        scheduled_payment_id: str | None = None,
        result: Iterable[PaymentAttemptResult] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[PaymentAttempt]:
        return await self._get_list(
            PaymentAttempt,
            f"{self._c(customer_id)}/payment-attempts",
            _params(
                scheduled_payment_id=scheduled_payment_id,
                result=[str(r) for r in result] if result else None,
                since=since,
                limit=limit,
            ),
        )

    async def list_payees(self, customer_id: str) -> list[Payee]:
        return await self._get_list(Payee, f"{self._c(customer_id)}/payees")

    async def get_payee(self, customer_id: str, payee_id: str) -> Payee:
        return await self._get_one(Payee, f"{self._c(customer_id)}/payees/{_seg(payee_id)}")

    # --- direct debits ----------------------------------------------------------------

    async def list_mandates(self, customer_id: str, *, status: MandateStatus | None = None) -> list[Mandate]:
        return await self._get_list(Mandate, f"{self._c(customer_id)}/mandates", _params(status=status))

    async def list_direct_debit_collections(
        self,
        customer_id: str,
        *,
        mandate_id: str | None = None,
        result: CollectionResult | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[DirectDebitCollection]:
        return await self._get_list(
            DirectDebitCollection,
            f"{self._c(customer_id)}/direct-debit-collections",
            _params(mandate_id=mandate_id, result=result, since=since, limit=limit),
        )


_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _seg(value: str) -> str:
    """One id as a path segment. Anything else (slashes, dots, queries) cannot escape the
    customer's path: it is treated as a record that does not exist."""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise BankingNotFound("invalid id", 404)
    return value
