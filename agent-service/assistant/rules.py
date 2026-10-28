"""Handoff rules (architecture section 4), as code.

Checked after triage and before answering, on every customer message. They are plain
functions of the customer's words, the triage result, the signal snapshot and the
conversation's failure count, so they are predictable (a persuasive customer cannot talk
the assistant out of handing off a fraud case), testable and measurable. No LLM is
involved and nothing here is shown to the model.

| Rule | Fires when |
|---|---|
| `human_requested` | the customer asks for a person |
| `sensitive_topic` | suspected fraud, a vulnerable customer, a formal complaint, bereavement |
| `needs_human_action` | the facts show a block or failure only ops can fix (fraud block, account not active, payee account closed, account closed by the originator), or the customer asks for a refund decision |
| `low_confidence` | triage is below the threshold, or the facts do not show the problem the customer describes |
| `repeated_failure` | the customer said "that didn't help" twice (👎 or in words), or the output guard rejected two answers (checked in the graph) |
"""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from core.banking.models import (
    AccountStatus,
    CardStatus,
    CardStatusReason,
    CollectionFailureCode,
    DeclineReason,
    PaymentFailureReason,
)
from core.enums import IssueType
from core.signals import SignalSnapshot
from core.triage import TriageResult

REPEATED_FAILURE_LIMIT = 2


class HandoffReason(StrEnum):
    HUMAN_REQUESTED = "human_requested"
    SENSITIVE_TOPIC = "sensitive_topic"
    NEEDS_HUMAN_ACTION = "needs_human_action"
    LOW_CONFIDENCE = "low_confidence"
    REPEATED_FAILURE = "repeated_failure"


class HandoffDecision(BaseModel):
    """Which rules fired, in the order of the table above (the first is the main reason)."""

    reasons: list[HandoffReason] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)  # one line per finding, for the case

    @property
    def handoff(self) -> bool:
        return bool(self.reasons)

    @property
    def primary(self) -> HandoffReason | None:
        return self.reasons[0] if self.reasons else None

    def add(self, reason: HandoffReason, detail: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.details.append(detail)

    def ordered(self) -> "HandoffDecision":
        order = list(HandoffReason)
        return HandoffDecision(reasons=sorted(self.reasons, key=order.index), details=self.details)


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


_STAFF = (
    r"(?:human|person|people|someone|somebody|agent|advis[eo]r|staff|representative|rep|operator|manager|"
    r"colleague|member\s+of\s+(?:staff|the\s+team)|customer\s+service)"
)
HUMAN_RE = _rx(
    r"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:(?:a|an|the|your|some|real|actual|live)\s+)*" + _STAFF + r"\b",
    r"\b(?:real|live|actual)\s+(?:human|person|agent|people)\b",
    r"\b(?:transfer|put|connect)\s+me\s+(?:to|through|with)\b",
    r"\b(?:human|person)\s+(?:please|pls)\b",
    r"\bcall\s+me(?:\s+back)?\b|\bcall\s*back\b",
    r"\bnot\s+(?:a|this|another)\s+(?:bot|chatbot|robot)\b",
    r"^\s*(?:agent|human|operator|representative)[\s!.?]*$",
)

FRAUD_RE = _rx(
    r"\bfraud\w*\b",
    r"\bscam\w*\b",
    r"\b(?:stolen|hacked|compromised|cloned)\b",
    r"\bunauthori[sz]ed\b",
    r"\bidentity\s+theft\b",
    r"\b(?:didn'?t|did\s+not|never)\s+(?:make|authori[sz]e|agree\s+to|recogni[sz]e)\b",
    r"\b(?:don'?t|do\s+not)\s+recogni[sz]e\b",
    r"\bsomeone\s+(?:else\s+)?(?:used|is\s+using|has\s+used|took)\s+my\b",
)
COMPLAINT_RE = _rx(
    r"\b(?:formal|official)\s+complaint\b",
    r"\b(?:make|file|raise|lodge|submit|open)\s+(?:a\s+)?complaint\b",
    r"\bi\s+(?:want|would\s+like|wish|need)\s+to\s+complain\b",
    r"\bcomplain(?:ing)?\s+about\b",
    r"\bombudsman\b",
)
BEREAVEMENT_RE = _rx(
    r"\b(?:passed\s+away|died|death|deceased|bereave\w*|funeral|widow\w*|probate)\b",
)
VULNERABLE_RE = _rx(
    r"\b(?:can'?t|cannot|struggling\s+to)\s+cope\b",
    r"\bsuicid\w*\b|\bself[\s-]harm\w*\b|\bkill\s+myself\b|\bend\s+my\s+life\b",
    r"\bmental\s+health\b",
    r"\b(?:domestic|financial|economic)\s+abuse\b|\bcoerc\w+\b",
    r"\b(?:in\s+hospital|terminal(?:ly)?\s+ill|seriously\s+ill)\b",
    r"\b(?:lost|losing)\s+my\s+job\b",
    r"\bcan'?t\s+afford\s+(?:food|to\s+eat|my\s+rent|to\s+live)\b",
    r"\bbailiffs?\b|\bdebt\s+collectors?\b",
)
REFUND_RE = _rx(
    r"\brefund\w*\b",
    r"\bchargeback\b",
    r"\bdispute\b",
    r"\bcompensat\w+\b",
    r"\b(?:my|the)\s+money\s+back\b",
)
NOT_HELPFUL_RE = _rx(
    r"\b(?:that|this|it|you)\s+(?:did\s*n[o']?t|does\s*n[o']?t|didnt|doesnt|did\s+not|does\s+not)\s+help\w*\b",
    r"\bnot\s+(?:very\s+)?help(?:ful|ing)\b",
    r"\b(?:no|zero)\s+help\b",
    r"\bunhelpful\b|\buseless\b",
    r"\bstill\s+(?:not\s+working|does\s*n[o']?t\s+work|declin\w+|fail\w*|broken|the\s+same)\b",
    r"\bthat'?s\s+(?:wrong|not\s+(?:right|it|true|correct))\b",
)


def said_not_helpful(message: str) -> bool:
    """The customer says the previous answer did not help ("that didn't help")."""
    return bool(NOT_HELPFUL_RE.search(message))


# --- facts that only ops can fix, per category -----------------------------------------

_INACTIVE_ACCOUNT = {AccountStatus.FROZEN.value, AccountStatus.CLOSED.value}


def _human_action_facts(snapshot: SignalSnapshot, category: IssueType) -> list[str]:
    found: list[str] = []
    inactive = [a for a in snapshot.accounts if a.status in _INACTIVE_ACCOUNT]
    if category is IssueType.CARD_DECLINE:
        for card in snapshot.cards:
            if card.status_reason == CardStatusReason.FRAUD_SUSPECTED:
                found.append(f"card ****{card.last4} is blocked for a security check")
            elif card.status == CardStatus.BLOCKED or card.status_reason == CardStatusReason.LOST_OR_STOLEN:
                found.append(f"card ****{card.last4} is blocked")
        reasons = {d.decline_reason for d in snapshot.recent_declines}
        if DeclineReason.FRAUD_RULE_TRIGGERED in reasons and not any("security" in f for f in found):
            found.append("recent declines were blocked for a security check")
        if DeclineReason.ACCOUNT_NOT_ACTIVE in reasons or inactive:
            found.append("the account is not active")
    elif category is IssueType.MISSED_PAYMENT:
        reasons = {p.failure_reason for p in snapshot.failed_payments}
        if PaymentFailureReason.PAYEE_ACCOUNT_CLOSED in reasons:
            found.append("a scheduled payment failed because the recipient's account is closed")
        if PaymentFailureReason.ACCOUNT_NOT_ACTIVE in reasons or inactive:
            found.append("the account is not active")
    elif category is IssueType.FAILED_DIRECT_DEBIT:
        codes = {c.failure_code for c in snapshot.failed_collections}
        if CollectionFailureCode.ACCOUNT_CLOSED_BY_ORIGINATOR in codes:
            found.append("a Direct Debit failed because the company closed the account it belongs to")
        if CollectionFailureCode.PAYER_ACCOUNT_CLOSED in codes or inactive:
            found.append("the account is not active")
    return found


def _facts_show(snapshot: SignalSnapshot, category: IssueType) -> bool:
    """The snapshot shows the kind of problem the customer describes."""
    if category is IssueType.CARD_DECLINE:
        return bool(snapshot.recent_declines) or any(c.status != CardStatus.ACTIVE for c in snapshot.cards)
    if category is IssueType.MISSED_PAYMENT:
        return bool(snapshot.failed_payments)
    if category is IssueType.FAILED_DIRECT_DEBIT:
        return bool(snapshot.failed_collections)
    return True


def evaluate_handoff_rules(
    message: str,
    triage: TriageResult,
    snapshot: SignalSnapshot | None,
    *,
    not_helpful_count: int,
) -> HandoffDecision:
    """All rules that fire for this customer message."""
    decision = HandoffDecision()

    if HUMAN_RE.search(message):
        decision.add(HandoffReason.HUMAN_REQUESTED, "the customer asked for a person")

    if FRAUD_RE.search(message):
        decision.add(HandoffReason.SENSITIVE_TOPIC, "the customer mentions suspected fraud")
    if COMPLAINT_RE.search(message):
        decision.add(HandoffReason.SENSITIVE_TOPIC, "the customer wants to make a complaint")
    if BEREAVEMENT_RE.search(message):
        decision.add(HandoffReason.SENSITIVE_TOPIC, "the customer mentions a bereavement")
    if VULNERABLE_RE.search(message):
        decision.add(HandoffReason.SENSITIVE_TOPIC, "the customer may be in a vulnerable situation")
    if snapshot is not None and snapshot.customer_vulnerable:
        decision.add(HandoffReason.SENSITIVE_TOPIC, "the customer is marked as vulnerable")

    if REFUND_RE.search(message):
        decision.add(HandoffReason.NEEDS_HUMAN_ACTION, "the customer asks for a refund or dispute decision")
    if snapshot is not None:
        for fact in _human_action_facts(snapshot, triage.category):
            decision.add(HandoffReason.NEEDS_HUMAN_ACTION, fact)

    if triage.below_threshold:
        decision.add(HandoffReason.LOW_CONFIDENCE, f"triage confidence {triage.confidence:.2f} is below the threshold")
    elif snapshot is not None and not snapshot.errors and not _facts_show(snapshot, triage.category):
        decision.add(
            HandoffReason.LOW_CONFIDENCE,
            f"the account shows no {triage.category.value.replace('_', ' ')} to explain the problem",
        )

    if not_helpful_count >= REPEATED_FAILURE_LIMIT:
        decision.add(
            HandoffReason.REPEATED_FAILURE, f"the customer said {not_helpful_count} times that it did not help"
        )

    return decision.ordered()
