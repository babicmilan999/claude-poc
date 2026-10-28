"""Masking before anything reaches an LLM or Langfuse."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from core.llm import mask_messages
from core.masking import REDACTED, langfuse_mask, mask_data, mask_text
from tests.fakes import FakeLLMProvider


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my card 4111 1111 1111 1111 was declined", "my card [CARD ****1111] was declined"),
        ("card 4000-0566-5566-5556", "card [CARD ****5556]"),
        ("visa 4222222222222", "visa [CARD ****2222]"),  # 13 digits
        ("maestro 6759649826438453", "maestro [CARD ****8453]"),
        ("account number 20261028", "account number ****1028"),  # date-like, but account wording
        ("acct no. 19991231 and a/c: 20200101", "acct no. ****1231 and a/c: ****0101"),
        ("transfer to 87654321", "transfer to ****4321"),  # not a valid date
        ("pan 5555555555554444.", "pan [CARD ****4444]."),
        ("amex 3782 822463 10005", "amex [CARD ****0005]"),
        ("sort code 12-34-56 account 12345678", "sort code **-**-** account ****5678"),
        ("sort code 12 34 56", "sort code **-**-**"),
        ("IBAN GB82 WEST 1234 5698 7654 32 please", "IBAN [IBAN ****5432] please"),
        ("IBAN GB82WEST12345698765432", "IBAN [IBAN ****5432]"),
        ("mail me at jane.doe+bank@example.co.uk", "mail me at [EMAIL]"),
        ("call 07700 900123 or +44 7700 900123", "call [PHONE] or [PHONE]"),
        ("the CVV is 123", "the CVV is ***"),
        ("cvc: 9876", "cvc: ***"),
    ],
)
def test_mask_text_masks_identifiers(raw: str, expected: str) -> None:
    assert mask_text(raw) == expected


@pytest.mark.parametrize(
    "safe",
    [
        "card c1c1c1c1-0000-4000-8000-000000000001 is frozen",
        "12345678-1234-4123-8123-123456789012",
        "paid 1234.56 GBP on 2026-10-28T09:00:00Z",
        "due 2026-10-26, amount -12.50",
        "card ending 4242, ****5678",
        "rule FR-12 at MCC 5411",
        "limit 100.00 and 1000000.00",
        "ref 1730000000000 on 20261028",  # epoch millis (not Luhn) and a YYYYMMDD date
        "card 4111-1111-1111-1234 is not a real number",  # fails the Luhn check
        "statement 20260131 to 20260228",
        "order 1234567890123456789",  # 19 digits, not Luhn
    ],
)
def test_mask_text_leaves_ids_dates_and_amounts(safe: str) -> None:
    assert mask_text(safe) == safe


def test_luhn() -> None:
    from core.masking import luhn_valid

    assert luhn_valid("4111111111111111") and luhn_valid("378282246310005")
    assert not luhn_valid("4111111111111112") and not luhn_valid("1730000000000")


def test_mask_by_key_does_not_need_luhn_or_context() -> None:
    """Known sensitive fields are masked whatever their value looks like."""
    masked = mask_data({"card_number": "4111111111111112", "account_number": "20261028"})
    assert masked == {"card_number": "[CARD ****1112]", "account_number": "****1028"}


def test_mask_text_is_idempotent() -> None:
    once = mask_text("4111 1111 1111 1111, 12-34-56, 12345678, a@b.com")
    assert mask_text(once) == once


def test_mask_data_masks_by_key_and_by_pattern() -> None:
    data = {
        "id": "a1a1a1a1-0000-4000-8000-000000000001",
        "sort_code": "12-34-56",
        "account_number": "12345678",
        "email": "alice@example.com",
        "note": "customer wrote 4111 1111 1111 1111",
        "nested": [{"account_number": 87654321, "payee": {"sort_code": "20-00-00"}}],
        "balance": "250.00",
    }
    masked = mask_data(data)
    assert masked["id"] == data["id"]
    assert masked["sort_code"] == "**-**-**"
    assert masked["account_number"] == "****5678"
    assert masked["email"] == "[EMAIL]"
    assert masked["note"] == "customer wrote [CARD ****1111]"
    assert masked["nested"][0]["account_number"] == "****4321"
    assert masked["nested"][0]["payee"]["sort_code"] == "**-**-**"
    assert masked["balance"] == "250.00"
    assert data["sort_code"] == "12-34-56", "mask_data must not mutate its input"


def test_mask_data_redacts_simulation_answer_keys() -> None:
    masked = mask_data({"expected_root_cause": "card_frozen_by_customer", "inner": {"expected_outcome": "self_serve"}})
    assert masked == {"expected_root_cause": REDACTED, "inner": {"expected_outcome": REDACTED}}


def test_langfuse_mask_masks_trace_payloads() -> None:
    payload = {"input": [{"role": "user", "content": "card 4111111111111111"}], "metadata": {"email": "x@y.com"}}
    masked = langfuse_mask(data=payload)
    assert masked["input"][0]["content"] == "card [CARD ****1111]"
    assert masked["metadata"]["email"] == "[EMAIL]"
    assert langfuse_mask(data="sort 12-34-56") == "sort **-**-**"


def test_mask_messages_covers_every_message_type() -> None:
    messages = [
        SystemMessage(content="You help customers."),
        HumanMessage(content="My card 4111 1111 1111 1111 failed"),
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"q": "acct 12345678"}, "id": "c1"}]),
        ToolMessage(content='{"sort_code": "12-34-56", "note": "IBAN GB82WEST12345698765432"}', tool_call_id="c1"),
        HumanMessage(content=[{"type": "text", "text": "email a@b.com"}]),
    ]
    masked = mask_messages(messages)
    assert masked[1].content == "My card [CARD ****1111] failed"
    assert isinstance(masked[2], AIMessage)
    assert masked[2].tool_calls[0]["args"] == {"q": "acct ****5678"}
    assert "12-34-56" not in masked[3].content and "GB82" not in masked[3].content
    assert masked[4].content[0]["text"] == "email [EMAIL]"  # type: ignore[index]
    assert messages[1].content == "My card 4111 1111 1111 1111 failed", "originals untouched"


async def test_llm_receives_masked_messages() -> None:
    """Every model from the provider masks before the request leaves the service."""
    llm = FakeLLMProvider({"assistant": ["ok", "streamed answer"]})
    model = llm.get("assistant")
    await model.ainvoke([HumanMessage(content="card 4111 1111 1111 1111, sort code 12-34-56")])
    sent = llm.fake("assistant").calls[0].text
    assert "4111" not in sent.replace("****1111", "") and "12-34-56" not in sent
    assert "[CARD ****1111]" in sent

    chunks = [c async for c in model.astream([HumanMessage(content="my email is a@b.com")])]
    assert "".join(str(c.content) for c in chunks) == "streamed answer"
    assert "a@b.com" not in llm.fake("assistant").calls[1].text
