"""Input guard (architecture section 4): simple rules first, then a `triage`-alias check
for prompt injection, abuse and off-topic messages.

Rules catch the obvious cases for free and cannot be talked around. If the model call
fails, the guard lets the message through (`method="fallback"`): the assistant is
read-only, its tools are scoped to the session's customer, and the output guard still
checks every answer, so blocking every customer while the model is down is the worse
failure.
"""

import logging
import re
from enum import StrEnum
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from core.llm import LLMProvider, ModelAlias, StructuredOutputError, invoke_structured
from core.masking import mask_text
from core.prompts import guardrails as prompts

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000


class InputVerdict(StrEnum):
    OK = "ok"
    INJECTION = "injection"
    ABUSE = "abuse"
    OFF_TOPIC = "off_topic"


class InputGuardOutput(BaseModel):
    """Screen a customer message."""

    verdict: InputVerdict = Field(description="ok, injection, abuse or off_topic.")
    reason: str = Field(description="One short reason.")


class InputGuardResult(BaseModel):
    verdict: InputVerdict
    reason: str
    method: Literal["rule", "llm", "fallback"]
    prompt_version: str = prompts.INPUT_GUARD_VERSION

    @property
    def allowed(self) -> bool:
        return self.verdict is InputVerdict.OK


# Only unambiguous injection phrasing is blocked by rule. Phrases ordinary payment
# questions also use ("another person", "a system message", "ignore my last message",
# "someone else's account" for a misdirected payment, "my customer ID") are left to the
# model check, which reads the whole message.
_ROLE_NOUNS = (
    r"(?:admin|administrator|root|superuser|developer|bank\s+employee|employee|staff\s+member|ops\s+agent|"
    r"operator|unrestricted|unfiltered|jailbroken|ai|assistant|bot|chatbot|model|dan)"
)
_INJECTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        # "ignore all previous instructions", "disregard the above prompt", "override your rules".
        # Not "ignore the previous instructions from the bank" (a letter, a payment instruction).
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:(?:all|any|of|the|everything)\s+)*"
            r"(?:(?:previous|prior|preceding|above|earlier|system|initial|original|developer|all|any)\s+)+"
            r"(?:instructions?|prompts?|guidelines|guardrails|directives|programming)\b"
            r"(?!\s+(?:from|in|on|about|for|of|to|i|we|you|the\s+bank)\b)"
            r"|\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|of\s+)*your\s+(?:own\s+|previous\s+)?"
            r"(?:instructions?|prompts?|rules|guidelines|guardrails|directives|programming)\b",
            re.I,
        ),
        "asks to ignore the assistant's instructions",
    ),
    (
        re.compile(
            r"\bsystem\s+prompts?\b|\b(?:system|developer)\s+instructions\b|\bdeveloper\s+(?:prompt|message)s?\b", re.I
        ),
        "asks about the system prompt",
    ),
    (
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|tell|give)\s+(?:me\s+|us\s+)?"
            r"(?:your|the\s+(?:hidden|secret|initial|original))\s+(?:hidden\s+|secret\s+|initial\s+|original\s+)?"
            r"(?:prompts?|instructions)\b",
            re.I,
        ),
        "asks to reveal the assistant's instructions",
    ),
    (
        re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are|pretend\s+(?:to\s+be|you\s+are)|act\s+as)\s+"
            r"(?:an?\s+|the\s+|my\s+)?(?:\w+\s+)?" + _ROLE_NOUNS + r"\b"
            r"|\byou\s+are\s+now\s+in\s+\w+\s+mode\b",
            re.I,
        ),
        "tries to change the assistant's role",
    ),
    (
        re.compile(r"\b(?:jailbreak\s+(?:mode|prompt)|dan\s+mode|developer\s+mode|do\s+anything\s+now)\b", re.I),
        "jailbreak attempt",
    ),
    (
        # "show me another customer's balance", "give me someone else's transactions"
        re.compile(
            r"\b(?:show|give|tell|send|list|read|view|access|look\s+up|pull\s+up|display)\s+(?:me\s+|us\s+)?"
            r"(?:the\s+|all\s+)?(?:(?:another|other|a\s+different)\s+(?:customer|user|account\s+holder)(?:'s|s')"
            r"|(?:someone|somebody|anyone|anybody)\s+else'?s\b)",
            re.I,
        ),
        "asks about another customer's data",
    ),
    (
        # The session decides the customer: `customer_id`, "use customer id ...", "customer id: <uuid>"
        re.compile(
            r"\bcustomer_id\b"
            r"|\b(?:use|set|switch\s+to|change|with|as)\s+(?:the\s+|my\s+)?customer[\s-]?id\b"
            r"|\bcustomer[\s-]?id\s*(?:[:=]|is|to)?\s*[0-9a-f]{8}-[0-9a-f]{4}-",
            re.I,
        ),
        "tries to choose a customer id",
    ),
]


def check_rules(message: str) -> InputGuardResult | None:
    if len(message) > MAX_MESSAGE_CHARS:
        return InputGuardResult(verdict=InputVerdict.INJECTION, reason="message too long", method="rule")
    for pattern, reason in _INJECTION_RULES:
        if pattern.search(message):
            return InputGuardResult(verdict=InputVerdict.INJECTION, reason=reason, method="rule")
    return None


async def check_input(llm: LLMProvider, message: str, *, config: RunnableConfig | None = None) -> InputGuardResult:
    ruled = check_rules(message)
    if ruled is not None:
        return ruled
    messages = [
        SystemMessage(content=prompts.INPUT_GUARD_SYSTEM),
        HumanMessage(content=prompts.INPUT_GUARD_USER.format(message=mask_text(message))),
    ]
    try:
        out = await invoke_structured(llm.get(ModelAlias.TRIAGE), InputGuardOutput, messages, config=config)
    except (StructuredOutputError, Exception) as exc:
        log.warning("input guard: model check unavailable (%s); allowing", exc)
        return InputGuardResult(verdict=InputVerdict.OK, reason="model check unavailable", method="fallback")
    return InputGuardResult(verdict=out.verdict, reason=out.reason, method="llm")


REFUSALS: dict[InputVerdict, str] = {
    InputVerdict.INJECTION: (
        "I can only help with your own payments, cards and account, and I can't change how I work. "
        "What can I help you with on your account?"
    ),
    InputVerdict.ABUSE: (
        "I'd like to help, but I can't continue with messages like that. "
        "If you tell me what went wrong with your payment or card, I'll look into it."
    ),
    InputVerdict.OFF_TOPIC: (
        "I can help with payments, cards, standing orders and Direct Debits on your account. "
        "Is there something there I can look into for you?"
    ),
}


def refusal_message(result: InputGuardResult) -> str:
    """A polite refusal or redirect for a message the guard did not allow."""
    return REFUSALS.get(result.verdict, REFUSALS[InputVerdict.OFF_TOPIC])
