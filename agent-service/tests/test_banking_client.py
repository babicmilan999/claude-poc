"""The banking client against the contract mock."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from core.banking import BankingClient, BankingError, BankingNotFound
from core.banking.models import CardStatus, CardStatusReason, DeclineReason, PaymentAttemptResult
from tests.banking_mock import ALICE, ALICE_CARD, BOB, BOB_CARD, NOW, UNKNOWN_CUSTOMER, ContractBankingMock


async def test_parses_records(banking: BankingClient) -> None:
    cards = await banking.list_cards(ALICE)
    assert cards[0].status is CardStatus.FROZEN and cards[0].status_reason is CardStatusReason.FROZEN_BY_CUSTOMER
    accounts = await banking.list_accounts(ALICE)
    assert accounts[0].balance == Decimal("250.00")
    declines = await banking.list_authorisations(ALICE, result="declined", since=NOW - timedelta(days=1))  # type: ignore[arg-type]
    assert [d.decline_reason for d in declines] == [DeclineReason.CARD_FROZEN, DeclineReason.CARD_FROZEN]


async def test_query_parameters_follow_the_contract(banking: BankingClient, banking_mock: ContractBankingMock) -> None:
    await banking.list_payment_attempts(
        ALICE, result=[PaymentAttemptResult.FAILED, PaymentAttemptResult.SKIPPED], limit=5
    )
    assert banking_mock.requests[-1].url.params["result"] == "failed,skipped"
    assert banking_mock.requests[-1].url.params["limit"] == "5"


async def test_unknown_customer_is_not_found(banking: BankingClient) -> None:
    with pytest.raises(BankingNotFound):
        await banking.list_cards(UNKNOWN_CUSTOMER)


async def test_another_customers_record_is_not_found(banking: BankingClient) -> None:
    """Records are nested under the customer, so a foreign id is a 404."""
    with pytest.raises(BankingNotFound):
        await banking.get_card_controls(ALICE, BOB_CARD)
    controls = await banking.get_card_controls(BOB, BOB_CARD)
    assert controls.card_id == BOB_CARD


@pytest.mark.parametrize("bad", ["../22222222-2222-4222-8222-222222222222", "a/b", "x?y=1", "", "..", "id#frag"])
async def test_ids_cannot_escape_the_customer_path(
    banking: BankingClient, banking_mock: ContractBankingMock, bad: str
) -> None:
    with pytest.raises(BankingNotFound):
        await banking.get_card_controls(ALICE, bad)
    with pytest.raises(BankingNotFound):
        await banking.list_cards(bad)
    assert banking_mock.requests == []


async def test_server_errors_raise_banking_error(banking: BankingClient, banking_mock: ContractBankingMock) -> None:
    banking_mock.fail_with["listCards"] = 500
    with pytest.raises(BankingError) as info:
        await banking.list_cards(ALICE)
    assert info.value.status_code == 500 and not isinstance(info.value, BankingNotFound)


async def test_unreachable_service_raises_banking_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = BankingClient("http://banking.test", transport=httpx.MockTransport(boom))
    with pytest.raises(BankingError, match="unreachable"):
        await client.list_cards(ALICE)
    await client.aclose()


async def test_only_get_requests(banking: BankingClient, banking_mock: ContractBankingMock) -> None:
    await banking.list_cards(ALICE)
    await banking.get_card_controls(ALICE, ALICE_CARD)
    assert {r.method for r in banking_mock.requests} == {"GET"}
