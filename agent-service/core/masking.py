"""Masking of sensitive data before anything reaches an LLM or Langfuse.

Two layers:
- by key: values of known sensitive fields (account numbers, sort codes, emails, ...)
  are masked whatever their format;
- by pattern: free text (customer messages, tool output rendered as text, model output)
  is scanned for card numbers, sort codes, account numbers, IBANs, emails, UK phone
  numbers and CVV codes.

Simulation answer keys (`expected_root_cause`, `expected_outcome`) are redacted too, as
a second line of defence: they must never reach an agent, a prompt or a trace.

Opaque record ids (UUIDs), money amounts, dates and timestamps are left alone; the tools
need the ids and they identify nothing outside the bank.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

REDACTED = "[REDACTED]"

# Field names whose values are always masked, whatever they look like.
_KEY_MASKERS: dict[str, Callable[[str], str]] = {}

# Field names that are never allowed to leave the service towards an LLM or a trace.
ANSWER_KEY_FIELDS = frozenset({"expected_root_cause", "expected_outcome"})

# --- patterns ----------------------------------------------------------------
# A boundary that keeps UUIDs, dates and decimal amounts out of the digit patterns.
_LB = r"(?<![\w\-.])"
_RB = r"(?![\w\-]|\.\d)"

_CARD_RE = re.compile(_LB + r"(?:\d[ \-]?){12,18}\d" + _RB)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_SORT_CODE_RE = re.compile(_LB + r"\d{2}[\- ]\d{2}[\- ]\d{2}" + _RB)
_ACCOUNT_NUMBER_RE = re.compile(_LB + r"\d{8}" + _RB)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_UK_PHONE_RE = re.compile(r"(?<![\w+])(?:\+44\s?7\d{3}|07\d{3})\s?\d{3}\s?\d{3}" + _RB)
_CVV_RE = re.compile(r"(?i)\b(cvv2?|cvc2?|security code)(\s*(?:is|:|=)?\s*)\d{3,4}\b")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def mask_card_number(value: str) -> str:
    digits = _digits(value)
    return f"[CARD ****{digits[-4:]}]" if len(digits) >= 4 else "[CARD]"


def mask_account_number(value: str) -> str:
    digits = _digits(value)
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


def mask_sort_code(_: str) -> str:
    return "**-**-**"


def mask_email(_: str) -> str:
    return "[EMAIL]"


def mask_phone(_: str) -> str:
    return "[PHONE]"


def mask_iban(value: str) -> str:
    compact = value.replace(" ", "")
    return f"[IBAN ****{compact[-4:]}]"


_KEY_MASKERS.update(
    {
        "account_number": mask_account_number,
        "sort_code": mask_sort_code,
        "email": mask_email,
        "phone": mask_phone,
        "phone_number": mask_phone,
        "card_number": mask_card_number,
        "pan": mask_card_number,
        "iban": mask_iban,
        "cvv": lambda _: "***",
        "cvc": lambda _: "***",
    }
)


def mask_text(text: str) -> str:
    """Mask sensitive identifiers in free text. Idempotent."""
    if not text:
        return text
    text = _CVV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    text = _IBAN_RE.sub(lambda m: mask_iban(m.group(0)), text)
    text = _CARD_RE.sub(lambda m: mask_card_number(m.group(0)), text)
    text = _UK_PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)
    text = _SORT_CODE_RE.sub(lambda m: mask_sort_code(m.group(0)), text)
    text = _ACCOUNT_NUMBER_RE.sub(lambda m: mask_account_number(m.group(0)), text)
    return text


def mask_data(data: Any) -> Any:
    """Mask a JSON-like structure (dicts, lists, strings, Pydantic models). Returns a copy."""
    if isinstance(data, BaseModel):
        return mask_data(data.model_dump(mode="json"))
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, Mapping):
        masked: dict[Any, Any] = {}
        for key, value in data.items():
            key_str = str(key).lower()
            if key_str in ANSWER_KEY_FIELDS:
                masked[key] = REDACTED
            elif key_str in _KEY_MASKERS and isinstance(value, str | int) and value != "":
                masked[key] = _KEY_MASKERS[key_str](str(value))
            else:
                masked[key] = mask_data(value)
        return masked
    if isinstance(data, list | tuple):
        return [mask_data(item) for item in data]
    return data


def langfuse_mask(*, data: Any, **_: Any) -> Any:
    """Mask function for the Langfuse client (`Langfuse(mask=...)`)."""
    try:
        return mask_data(data)
    except Exception:  # never let masking errors leak the original data
        return REDACTED
