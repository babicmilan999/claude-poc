"""A scripted "perfect model" for end-to-end runs on the fake LLM (`tests/fakes.py`).

Every alias is scripted with callables that look at the (already masked) messages the
model was sent and answer like a well-behaved model would: the guards pass, triage reads
the customer's words, investigators call their playbook's tools, the diagnosis names the
root cause the tool evidence shows, the simulated customer opens with its phrasing and
leaves satisfied after an answer. It knows nothing about any scenario's answer key: it
reads only its prompts, so a correct score means the facts flowed through the agents.

Which call is which is decided by the system prompt, compared with the packages' own
prompt constants.
"""

import re
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from assistant import prompts as assistant_prompts
from core.enums import IssueType, RootCause
from core.guardrails.input_guard import InputGuardOutput, InputVerdict
from core.guardrails.output_guard import OutputGuardOutput
from core.prompts import guardrails as guard_prompts
from core.prompts import triage as triage_prompts
from core.triage import TriageOutput
from evals import prompts as eval_prompts
from evals.scorers import GroundednessVerdict
from investigation import prompts as investigation_prompts
from investigation.state import DiagnosisOutput, EvidenceAssessment
from simulation import persona as persona_prompts
from simulation.persona import CustomerTurn
from tests.fakes import FakeLLMProvider, structured, text, tool_calls

ANSWER = "Here is what I found on your account and what you can do about it in the app."
FINDINGS = "Findings: the tool results above show what failed and why; see the cited evidence."
EXPLANATION = "The cited evidence shows the cause of the failure."
SUMMARY = "Customer reported a payment problem; the account signals need an ops action."

PLAYBOOK_TOOLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "card_decline": [("get_cards", {}), ("get_card_authorisations", {"result": "declined"}), ("get_fraud_flags", {})],
    "missed_payment": [("get_scheduled_payments", {}), ("get_payment_attempts", {})],
    "failed_direct_debit": [("get_direct_debit_collections", {"result": "failed"}), ("get_mandates", {})],
    "other": [("get_cards", {}), ("get_direct_debit_collections", {})],
}

# Which reason codes in the evidence point at which root cause, per issue type (first wins).
EVIDENCE_CODES: dict[IssueType, list[tuple[str, RootCause]]] = {
    IssueType.CARD_DECLINE: [
        ("FRAUD_RULE_TRIGGERED", RootCause.FRAUD_RULE_TRIGGERED),
        ("CARD_FROZEN", RootCause.CARD_FROZEN_BY_CUSTOMER),
        ("CARD_EXPIRED", RootCause.CARD_EXPIRED),
        ("CONTACTLESS_LIMIT_EXCEEDED", RootCause.CONTACTLESS_LIMIT_EXCEEDED),
        ("MERCHANT_CATEGORY_BLOCKED", RootCause.MERCHANT_CATEGORY_BLOCKED),
        ("INSUFFICIENT_FUNDS", RootCause.INSUFFICIENT_FUNDS),
    ],
    IssueType.MISSED_PAYMENT: [
        ("PAYEE_ACCOUNT_CLOSED", RootCause.PAYEE_ACCOUNT_CLOSED),
        ("DAILY_PAYMENT_LIMIT_EXCEEDED", RootCause.DAILY_PAYMENT_LIMIT_EXCEEDED),
        ("INSUFFICIENT_FUNDS", RootCause.INSUFFICIENT_FUNDS),
    ],
    IssueType.FAILED_DIRECT_DEBIT: [
        ("ACCOUNT_CLOSED_BY_ORIGINATOR", RootCause.ACCOUNT_CLOSED_BY_ORIGINATOR),
        ("MANDATE_CANCELLED", RootCause.MANDATE_CANCELLED_BY_CUSTOMER),
        ("INSUFFICIENT_FUNDS", RootCause.INSUFFICIENT_FUNDS),
    ],
}

_EV_ID = re.compile(r"(?<![\w-])ev-\d+\b")


def _content(message: BaseMessage) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def _system(messages: list[BaseMessage]) -> str:
    return _content(messages[0]) if messages and isinstance(messages[0], SystemMessage) else ""


def _starts(system: str, prompt: str) -> bool:
    return system.startswith(prompt[:60])


def _all_text(messages: list[BaseMessage]) -> str:
    return "\n".join(_content(m) for m in messages)


def classify(words: str) -> IssueType:
    lowered = words.lower()
    if "direct debit" in lowered or "letting agent" in lowered or "collection" in lowered:
        return IssueType.FAILED_DIRECT_DEBIT
    if "declin" in lowered or "card" in lowered:
        return IssueType.CARD_DECLINE
    if "standing order" in lowered or "rent" in lowered or "landlord" in lowered:
        return IssueType.MISSED_PAYMENT
    return IssueType.OTHER


def _between(text_: str, start: str, end: str) -> str:
    head = text_.split(start, 1)
    return head[1].split(end, 1)[0] if len(head) == 2 else text_


# --- per alias ---------------------------------------------------------------------------


def simulator(messages: list[BaseMessage]) -> AIMessage:
    system, last = _system(messages), _content(messages[-1])
    if persona_prompts.FIRST_USER in last:
        phrasing = _between(system, "How you might put it:\n", "\n\n")
        knows = _between(system, "What you know (you know nothing else about the problem):\n", "\n\n")
        bullets = [b[2:].strip() for b in (phrasing + "\n" + knows).splitlines() if b.startswith("- ")]
        first = next((b for b in bullets if b != "in your own words"), "something went wrong with a payment")
        return structured(CustomerTurn(message=f"Hello, {first}. Why?"))
    return structured(CustomerTurn(done=True, satisfied=True))


def triage_alias(messages: list[BaseMessage]) -> AIMessage:
    system = _system(messages)
    if _starts(system, guard_prompts.INPUT_GUARD_SYSTEM):
        return structured(InputGuardOutput(verdict=InputVerdict.OK, reason="a payments question"))
    if _starts(system, triage_prompts.SYSTEM):
        words = _between(_content(messages[-1]), "<complaint>", "</complaint>")
        return structured(TriageOutput(category=classify(words), confidence=0.9, reasoning="the customer's words"))
    raise AssertionError(f"unexpected triage-alias call: {system[:80]!r}")


def judge_alias(messages: list[BaseMessage]) -> AIMessage:
    system = _system(messages)
    if _starts(system, guard_prompts.OUTPUT_GUARD_SYSTEM):
        return structured(
            OutputGuardOutput(
                grounded=True, unsupported_claims=[], financial_advice=False, internal_details=False, reason="ok"
            )
        )
    if _starts(system, eval_prompts.GROUNDEDNESS_SYSTEM):
        return structured(GroundednessVerdict(claims=2, supported=2, reason="both claims are in the sources"))
    raise AssertionError(f"unexpected judge-alias call: {system[:80]!r}")


def assistant_alias(messages: list[BaseMessage]) -> AIMessage:
    system = _system(messages)
    if _starts(system, assistant_prompts.SUMMARY_SYSTEM):
        return text(SUMMARY)
    if any(isinstance(m, ToolMessage) for m in messages):
        return text(ANSWER)
    return tool_calls(("get_my_cards", {}), ("get_my_direct_debits", {}), ("get_my_scheduled_payments", {}))


def _common_prefix(texts: list[str]) -> str:
    first, last = min(texts), max(texts)
    n = 0
    while n < min(len(first), len(last)) and first[n] == last[n]:
        n += 1
    return first[:n]


def reasoning_alias(messages: list[BaseMessage]) -> AIMessage:
    system = _system(messages)
    common = _common_prefix(list(investigation_prompts.PLAYBOOKS.values()))
    for issue, playbook in investigation_prompts.PLAYBOOKS.items():
        if system.startswith(common[:60]) and playbook[len(common) :].strip()[:40] in system:
            if any(isinstance(m, ToolMessage) for m in messages):
                return text(FINDINGS)
            return tool_calls(*PLAYBOOK_TOOLS[issue])
    body = _all_text(messages[1:])
    ids = sorted(set(_EV_ID.findall(body)), key=lambda i: int(i.split("-")[1]))
    if _starts(system, investigation_prompts.EVIDENCE_CHECK_SYSTEM):
        return structured(EvidenceAssessment(enough=bool(ids), evidence_ids=ids))
    if _starts(system, investigation_prompts.DIAGNOSE_SYSTEM):
        match = re.search(r"Issue type so far: (\w+)", body)
        issue = IssueType(match.group(1)) if match and match.group(1) in IssueType else IssueType.OTHER
        evidence = body.split("Evidence:", 1)[-1]
        root = next((cause for code, cause in EVIDENCE_CODES.get(issue, []) if code in evidence), RootCause.UNKNOWN)
        return structured(
            DiagnosisOutput(
                issue_type=issue,
                root_cause=root,
                confidence=0.9,
                evidence_ids=ids or ["ev-1"],
                recommended_action="Explain the cause to the customer.",
            )
        )
    if _starts(system, investigation_prompts.EXPLAIN_SYSTEM):
        return text(EXPLANATION)
    raise AssertionError(f"unexpected reasoning-alias call: {system[:80]!r}")


ROUTERS: dict[str, Callable[[list[BaseMessage]], AIMessage]] = {
    "simulator": simulator,
    "triage": triage_alias,
    "judge": judge_alias,
    "assistant": assistant_alias,
    "reasoning": reasoning_alias,
}


def perfect_models(copies: int = 2000) -> FakeLLMProvider:
    """A `FakeLLMProvider` whose aliases all answer through the routers above."""
    llm = FakeLLMProvider()
    for alias, router in ROUTERS.items():
        llm.script(alias, *[router] * copies)
    return llm


def calls_by_alias(llm: FakeLLMProvider) -> dict[str, list[Any]]:
    return {alias: list(llm.fake(alias).calls) for alias in ROUTERS}
