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


_INJECTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules|prompt|guidelines|guardrails)\b",
            re.I | re.S,
        ),
        "asks to ignore the assistant's instructions",
    ),
    (re.compile(r"\b(system|developer)\s+(prompt|message|instructions?)\b", re.I), "asks about the system prompt"),
    (
        re.compile(r"\b(reveal|show|print|repeat)\b.{0,30}\b(your|the)\s+(prompt|instructions?)\b", re.I | re.S),
        "asks to reveal the assistant's instructions",
    ),
    (
        re.compile(
            r"\b(you are now|pretend (to be|you are)|act as (an?|the) (admin|developer|bank employee|ops agent))\b",
            re.I,
        ),
        "tries to change the assistant's role",
    ),
    (re.compile(r"\b(jailbreak|dan mode|developer mode)\b", re.I), "jailbreak attempt"),
    (
        re.compile(r"\b(another|other|different)\s+(customer|person|user|account holder)('?s)?\b", re.I),
        "asks about another customer's data",
    ),
    (
        re.compile(r"\bsomeone else'?s\s+(account|card|data|details|balance|payments?)\b", re.I),
        "asks about someone else's data",
    ),
    (re.compile(r"\bcustomer[\s_-]?id\b", re.I), "tries to choose a customer id"),
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
