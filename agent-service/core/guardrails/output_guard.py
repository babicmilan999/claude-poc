"""Output guard (architecture section 4): rules, then a `judge`-alias check that every
statement is backed by a source, with no financial advice and no internal details.

It fails closed: if the judge cannot be reached or answers nonsense, the answer does not
pass (the assistant regenerates once, then hands off).
"""

import json
import logging
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from core.banking.models import CardStatusReason, CollectionFailureCode, DeclineReason, PaymentFailureReason
from core.llm import LLMProvider, ModelAlias, StructuredOutputError, invoke_structured
from core.masking import mask_data, mask_text
from core.prompts import guardrails as prompts

log = logging.getLogger(__name__)

INTERNAL_CODES = frozenset(
    str(code)
    for enum in (DeclineReason, PaymentFailureReason, CollectionFailureCode, CardStatusReason)
    for code in enum
    if "_" in str(code)  # bare words like EXPIRED are ordinary English
)
_INTERNAL_CODE_RE = re.compile(r"\b(" + "|".join(sorted(INTERNAL_CODES, key=len, reverse=True)) + r")\b")
_INTERNAL_TEXT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bFR-\d+\b"), "mentions a fraud rule id"),
    (re.compile(r"\bfraud[\s-]+rules?\b", re.I), "mentions fraud rules"),
    (re.compile(r"\brisk[\s-]+scores?\b", re.I), "mentions a risk score"),
]


class Source(BaseModel):
    """Something an answer may rely on."""

    kind: Literal["tool", "article"]
    name: str  # tool name or article title
    content: Any


class OutputGuardOutput(BaseModel):
    """Check an answer against its sources."""

    grounded: bool = Field(description="Every factual statement is backed by a source.")
    unsupported_claims: list[str] = Field(default_factory=list, description="Statements without a source.")
    financial_advice: bool = Field(description="The answer gives financial advice.")
    internal_details: bool = Field(description="The answer reveals internal details.")
    reason: str = Field(description="One short reason.")


class OutputGuardResult(BaseModel):
    passed: bool
    grounded: bool
    financial_advice: bool
    internal_details: bool
    unsupported_claims: list[str]
    reasons: list[str]
    method: Literal["rule", "llm", "fallback"]
    prompt_version: str = prompts.OUTPUT_GUARD_VERSION


def check_output_rules(answer: str) -> list[str]:
    """Problems detectable without a model: internal codes, rule ids, unmasked identifiers."""
    problems: list[str] = []
    codes = sorted(set(_INTERNAL_CODE_RE.findall(answer)))
    if codes:
        problems.append(f"contains internal codes: {', '.join(codes)}")
    for pattern, reason in _INTERNAL_TEXT_RULES:
        if pattern.search(answer):
            problems.append(reason)
    if mask_text(answer) != answer:
        problems.append("contains an unmasked card number or account identifier")
    return problems


def render_sources(sources: list[Source]) -> str:
    if not sources:
        return "(no sources)"
    parts = []
    for i, source in enumerate(sources, start=1):
        content = (
            source.content if isinstance(source.content, str) else json.dumps(mask_data(source.content), default=str)
        )
        parts.append(f"[{i}] {source.kind}: {source.name}\n{mask_text(content)}")
    return "\n\n".join(parts)


async def check_output(
    llm: LLMProvider,
    answer: str,
    sources: list[Source],
    *,
    config: RunnableConfig | None = None,
) -> OutputGuardResult:
    problems = check_output_rules(answer)
    if problems:
        return OutputGuardResult(
            passed=False,
            grounded=True,
            financial_advice=False,
            internal_details=True,
            unsupported_claims=[],
            reasons=problems,
            method="rule",
        )
    messages = [
        SystemMessage(content=prompts.OUTPUT_GUARD_SYSTEM),
        HumanMessage(
            content=prompts.OUTPUT_GUARD_USER.format(sources=render_sources(sources), answer=mask_text(answer))
        ),
    ]
    try:
        out = await invoke_structured(llm.get(ModelAlias.JUDGE), OutputGuardOutput, messages, config=config)
    except (StructuredOutputError, Exception) as exc:
        log.warning("output guard: judge unavailable (%s); failing closed", exc)
        return OutputGuardResult(
            passed=False,
            grounded=False,
            financial_advice=False,
            internal_details=False,
            unsupported_claims=[],
            reasons=["output check unavailable"],
            method="fallback",
        )
    passed = out.grounded and not out.financial_advice and not out.internal_details
    reasons = [] if passed else [out.reason]
    return OutputGuardResult(
        passed=passed,
        grounded=out.grounded,
        financial_advice=out.financial_advice,
        internal_details=out.internal_details,
        unsupported_claims=out.unsupported_claims,
        reasons=reasons,
        method="llm",
    )
