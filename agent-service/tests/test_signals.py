"""Signal snapshot: plain API calls, no LLM."""

from core.banking import BankingClient
from core.enums import IssueType
from core.signals import build_signal_snapshot
from tests.banking_mock import ALICE, BOB, NOW, ContractBankingMock


async def test_alice_snapshot(banking: BankingClient, clock) -> None:
    snapshot = await build_signal_snapshot(banking, ALICE, clock=clock)
    assert snapshot.taken_at == NOW and snapshot.errors == []
    assert snapshot.customer_status == "active"
    assert [c.status for c in snapshot.cards] == ["frozen"]
    assert [d.decline_reason for d in snapshot.recent_declines] == ["CARD_FROZEN", "CARD_FROZEN"]
    assert [p.failure_reason for p in snapshot.failed_payments] == ["INSUFFICIENT_FUNDS"]
    assert snapshot.failed_payments[0].payee_name == "Landlord Ltd"
    assert [c.failure_code for c in snapshot.failed_collections] == ["MANDATE_CANCELLED"]
    # Most recent evidence first: declines today, DD on the 26th, standing order on the 25th.
    assert snapshot.signal_categories() == [
        IssueType.CARD_DECLINE,
        IssueType.FAILED_DIRECT_DEBIT,
        IssueType.MISSED_PAYMENT,
    ]
    assert not snapshot.has_fraud_block()


async def test_snapshot_uses_the_lookback_window(
    banking: BankingClient, banking_mock: ContractBankingMock, clock
) -> None:
    snapshot = await build_signal_snapshot(banking, ALICE, clock=clock, lookback_days=2)
    assert len(snapshot.recent_declines) == 2
    assert snapshot.failed_payments == [] and snapshot.failed_collections == []
    declined = next(r for r in banking_mock.requests if r.url.path.endswith("/authorisations"))
    assert declined.url.params["result"] == "declined" and declined.url.params["since"].startswith("2026-10-26")


async def test_fraud_block_is_visible_to_ops_but_not_in_the_customer_view(banking: BankingClient, clock) -> None:
    snapshot = await build_signal_snapshot(banking, BOB, clock=clock)
    assert snapshot.has_fraud_block()
    assert "FRAUD_RULE_TRIGGERED" in snapshot.to_prompt(audience="ops")
    customer_text = snapshot.to_prompt(audience="customer")
    assert "blocked for a security check" in customer_text
    for internal in ("FRAUD", "FR-12", "risk", "status_reason"):
        assert internal not in customer_text
        assert internal not in str(snapshot.customer_safe())


async def test_snapshot_masks_account_numbers(banking: BankingClient, clock) -> None:
    snapshot = await build_signal_snapshot(banking, ALICE, clock=clock)
    for text in (snapshot.to_prompt(), snapshot.to_prompt(audience="customer"), snapshot.model_dump_json()):
        assert "12345678" not in text and "12-34-56" not in text
    assert "****5678" in snapshot.to_prompt()


async def test_a_failed_lookup_does_not_sink_the_snapshot(
    banking: BankingClient, banking_mock: ContractBankingMock, clock
) -> None:
    banking_mock.fail_with["listDirectDebitCollections"] = 500
    snapshot = await build_signal_snapshot(banking, ALICE, clock=clock)
    assert snapshot.errors == ["collections"]
    assert snapshot.recent_declines and snapshot.failed_collections == []
    assert "lookups that failed: collections" in snapshot.to_prompt()


async def test_empty_account_has_no_signals(banking: BankingClient, banking_mock: ContractBankingMock, clock) -> None:
    world = banking_mock.world[BOB]
    world.authorisations.clear()
    world.cards[0]["status"] = "active"
    world.cards[0]["status_reason"] = None
    snapshot = await build_signal_snapshot(banking, BOB, clock=clock)
    assert snapshot.is_empty and snapshot.signal_categories() == []
    assert "recent card declines: 0" in snapshot.to_prompt()
    assert snapshot.accounts[0].low_balance is False
