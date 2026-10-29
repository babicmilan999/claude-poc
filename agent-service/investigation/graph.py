"""The investigation graph (architecture section 6).

    START → intake ─category provided?─▶ verify ─confirmed─▶ route
                   └─no──────────────▶ triage ◀─doubtful─┘     │
                                         │                      │
                                         └──────────▶ route ────┤
    route ─auto run, type not enabled─▶ park → END
    route ─card_decline / missed_payment / failed_direct_debit / other─▶ investigator
    investigator → evidence_check ─enough─▶ diagnose ─▶ review (interrupt)
                                  ─not enough, budget left─▶ triage
                                  ─not enough, budget spent─▶ escalate ─▶ review
    review ─accepted / corrected─▶ finalize → END
           ─rejected with hint──▶ triage (same thread, hint in state, fresh budget)

- Human review cannot be skipped: `finalize` is reachable only from `review`, and
  `diagnose` and `escalate` both lead to `review`.
- The evidence check gates the diagnosis: `diagnose` is reachable only from
  `evidence_check`, which needs tool evidence from this investigation.
- Investigators get only their issue type's toolset and a step budget per pass; the run
  has a total budget (`INVESTIGATION_MAX_STEPS`) per review round.
- LLM calls use the `triage` alias (triage) and `reasoning` (everything else), through
  `MaskedChatModel`; prompts are masked again when they are built.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer, interrupt

from core.banking import BankingClient, BankingError
from core.enums import EvidenceOrigin, IssueType, Verdict
from core.llm import LLMProvider, ModelAlias, StructuredOutputError, invoke_structured
from core.masking import mask_text
from core.signals import SignalSnapshot, build_signal_snapshot
from core.tools import ops_tools_for
from core.tools.common import Clock, utc_now
from core.triage import triage as run_triage
from investigation import prompts
from investigation.common import (
    MaskedTokenStream,
    emit,
    evidence_lines,
    normalise_handoff_evidence,
    step_started,
)
from investigation.investigator import build_investigator, findings_of
from investigation.settings import InvestigationSettings
from investigation.state import (
    INVESTIGATOR_NODES,
    DiagnosisOutput,
    EvidenceAssessment,
    InvestigationState,
    ReviewDecision,
)
from investigation.store import IntakeData

log = logging.getLogger(__name__)

Score = Callable[[str, str, float, str | None], None]

# Which customer-scoped tool (the assistant's handoff evidence) supports which category.
HANDOFF_TOOL_CATEGORIES: dict[str, IssueType] = {
    "get_my_cards": IssueType.CARD_DECLINE,
    "get_my_recent_declines": IssueType.CARD_DECLINE,
    "get_my_card_controls": IssueType.CARD_DECLINE,
    "get_my_scheduled_payments": IssueType.MISSED_PAYMENT,
    "get_my_direct_debits": IssueType.FAILED_DIRECT_DEBIT,
}


@dataclass
class GraphDeps:
    llm: LLMProvider
    banking: BankingClient
    load_intake: Callable[[uuid.UUID], Awaitable[IntakeData]]
    settings: InvestigationSettings
    auto_investigate_types: frozenset[IssueType]
    triage_threshold: float
    clock: Clock = utc_now
    score: Score | None = None  # Langfuse scores: (investigation_id, name, value, comment)
    tools_for: Callable[[str, IssueType], list[BaseTool]] | None = field(default=None)

    def toolset(self, customer_id: str, issue_type: IssueType) -> list[BaseTool]:
        if self.tools_for is not None:
            return self.tools_for(customer_id, issue_type)
        return ops_tools_for(self.banking, customer_id, issue_type, clock=self.clock)


# --- prompt text ---------------------------------------------------------------------


def case_text(state: InvestigationState, *, findings: bool = True, hint: bool = True) -> str:
    parts = [f"Customer: {state.get('customer_profile') or 'unknown'}"]
    if state.get("complaint"):
        parts.append(f"Complaint:\n{mask_text(str(state['complaint']).strip())}")
    if state.get("transcript_summary"):
        parts.append(f"Assistant handoff summary:\n{mask_text(str(state['transcript_summary']).strip())}")
    if state.get("trigger_payload"):
        parts.append(f"Event payload:\n{mask_text(json.dumps(state['trigger_payload'], default=str))}")
    if state.get("provided_category"):
        parts.append(f"Category given by the assistant: {state['provided_category']}")
    if findings and state.get("findings"):
        parts.append(f"Findings so far:\n{mask_text(str(state['findings']))}")
    if hint and state.get("reviewer_feedback"):
        parts.append(f"Reviewer hint:\n{mask_text(str(state['reviewer_feedback']))}")
    return "\n\n".join(parts)


def _snapshot(state: InvestigationState) -> SignalSnapshot | None:
    raw = state.get("snapshot")
    return SignalSnapshot.model_validate(raw) if raw else None


def _snapshot_text(state: InvestigationState) -> str:
    snapshot = _snapshot(state)
    return snapshot.to_prompt(audience="ops") if snapshot else "Signal snapshot: not available."


def _investigation_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return [e["id"] for e in evidence if e.get("origin") == EvidenceOrigin.INVESTIGATION.value]


def _remaining_steps(state: InvestigationState, settings: InvestigationSettings) -> int:
    used = state.get("steps_used", 0) - state.get("budget_base", 0)
    return settings.investigation_max_steps - used


# --- graph -----------------------------------------------------------------------------


def build_graph(deps: GraphDeps, *, checkpointer: Checkpointer = None) -> CompiledStateGraph:
    settings = deps.settings

    async def intake(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "intake", 0)
        data = await deps.load_intake(uuid.UUID(state["case_id"]))
        customer_id = str(data.customer_id)
        profile = data.customer_name or "unknown"
        try:
            customer = await deps.banking.get_customer(customer_id)
            profile = f"{customer.name} (status {customer.status}{', vulnerable' if customer.vulnerable else ''})"
        except BankingError:
            log.warning("investigation %s: customer profile not available", state.get("investigation_id"))
        snapshot = await build_signal_snapshot(deps.banking, customer_id, clock=deps.clock)
        summary = data.summary
        if not summary and data.transcript:
            said = [content for role, content in data.transcript if role == "customer"][-5:]
            summary = "Customer said: " + " | ".join(said) if said else None
        return {
            "customer_id": customer_id,
            "customer_profile": profile,
            "complaint": data.complaint,
            "transcript_summary": summary,
            "trigger_payload": data.trigger_payload,
            "provided_category": data.category.value if data.category else None,
            "snapshot": snapshot.model_dump(mode="json"),
            "evidence": normalise_handoff_evidence(data.handoff_evidence),
            "issue_type": None,
            "category_changed": False,
            "steps_used": 0,
            "budget_base": 0,
            "passes": 0,
        }

    def has_category(state: InvestigationState) -> str:
        provided = state.get("provided_category")
        return "verify" if provided and provided != IssueType.OTHER.value else "triage"

    async def verify(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        """Check the assistant's category against the signal snapshot and its evidence
        (code, no LLM call)."""
        await step_started(config, "verify", state.get("steps_used", 0))
        provided = IssueType(str(state.get("provided_category")))
        snapshot = _snapshot(state)
        signals = snapshot.signal_categories() if snapshot else []
        handoff_support = {
            HANDOFF_TOOL_CATEGORIES[e["tool"]]
            for e in state.get("evidence", [])
            if e.get("origin") == EvidenceOrigin.ASSISTANT.value and e.get("tool") in HANDOFF_TOOL_CATEGORIES
        }
        if provided in signals:
            confidence = 0.95 if signals[0] is provided else 0.85
            reasoning = f"The signal snapshot shows recent {provided.value} activity."
        elif not signals and provided in handoff_support:
            confidence = 0.7
            reasoning = "No contradicting signals; the assistant's evidence supports the category."
        else:
            return {"classification_method": None}
        await emit(
            config,
            "classified",
            {
                "issue_type": provided.value,
                "confidence": confidence,
                "method": "verified",
                "previous_category": provided.value,
                "reasoning": reasoning,
            },
        )
        return {"issue_type": provided.value, "classification_method": "verified", "category_changed": False}

    def after_verify(state: InvestigationState) -> str:
        return "route" if state.get("classification_method") == "verified" else "triage"

    async def triage(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "triage", state.get("steps_used", 0))
        result = await run_triage(
            deps.llm, case_text(state), _snapshot(state), threshold=deps.triage_threshold, config=config
        )
        provided = state.get("provided_category")
        previous = state.get("issue_type") or provided
        await emit(
            config,
            "classified",
            {
                "issue_type": result.category.value,
                "confidence": result.confidence,
                "method": "triaged",
                "previous_category": previous,
                "reasoning": mask_text(result.reasoning),
            },
        )
        return {
            "issue_type": result.category.value,
            "classification_method": "triaged",
            "category_changed": provided is not None and result.category.value != provided,
        }

    async def route(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "route", state.get("steps_used", 0))
        return {}

    def choose_route(state: InvestigationState) -> str:
        issue_type = IssueType(state.get("issue_type") or IssueType.OTHER.value)
        first_pass = state.get("passes", 0) == 0 and not state.get("reviewer_feedback")
        if state.get("trigger") == "auto" and first_pass and issue_type not in deps.auto_investigate_types:
            return "park"
        return INVESTIGATOR_NODES[issue_type]

    async def park(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "park", state.get("steps_used", 0))
        return {"outcome": "parked"}

    def make_investigator_node(issue_type: IssueType) -> Callable[..., Awaitable[dict[str, Any]]]:
        subgraph = build_investigator(issue_type, llm=deps.llm, tools_for=deps.toolset)
        node_name = INVESTIGATOR_NODES[issue_type]
        per_pass = (
            settings.generic_investigator_step_budget
            if issue_type is IssueType.OTHER
            else settings.investigator_step_budget
        )

        async def investigator(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
            steps_used = state.get("steps_used", 0)
            await step_started(config, node_name, steps_used)
            budget = min(per_pass, _remaining_steps(state, settings))
            passes = state.get("passes", 0) + 1
            if budget <= 0:
                return {"passes": passes, "findings": state.get("findings")}
            evidence = state.get("evidence", [])
            briefing = prompts.BRIEFING_TEMPLATE.format(
                issue_type=issue_type.value,
                case_text=case_text(state, hint=False),
                snapshot=_snapshot_text(state)
                + ("\n\nEvidence so far:\n" + evidence_lines(evidence) if evidence else ""),
                feedback=prompts.FEEDBACK_TEMPLATE.format(hint=mask_text(str(state.get("reviewer_feedback"))))
                if state.get("reviewer_feedback")
                else "",
            )
            result = await subgraph.ainvoke(
                {
                    "customer_id": state["customer_id"],
                    "issue_type": issue_type.value,
                    "briefing": briefing,
                    "messages": [],
                    "evidence": [],
                    "taken_ids": [e["id"] for e in evidence],
                    "steps": 0,
                    "budget": budget,
                },
                config,
            )
            return {
                "evidence": result.get("evidence", []),
                "steps_used": steps_used + result.get("steps", 0),
                "passes": passes,
                "findings": findings_of(result),  # type: ignore[arg-type]
            }

        investigator.__name__ = node_name
        return investigator

    async def evidence_check(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "evidence_check", state.get("steps_used", 0))
        evidence = state.get("evidence", [])
        own = set(_investigation_ids(evidence))
        if not own:
            return {"assessment": {"enough": False, "evidence_ids": [], "missing": "No tool evidence yet."}}
        messages = [
            SystemMessage(content=prompts.EVIDENCE_CHECK_SYSTEM),
            HumanMessage(
                content=f"{case_text(state, hint=True)}\n\nIssue type: {state.get('issue_type')}\n\n"
                f"Evidence:\n{evidence_lines(evidence, with_data=True)}"
            ),
        ]
        try:
            out = await invoke_structured(deps.llm.get(ModelAlias.REASONING), EvidenceAssessment, messages, config)
        except StructuredOutputError as exc:
            log.warning("evidence check: unusable model output (%s)", exc)
            out = EvidenceAssessment(enough=False, missing="The evidence check could not be read.")
        known = {e["id"] for e in evidence}
        cited = [i for i in out.evidence_ids if i in known]
        enough = out.enough and any(i in own for i in cited)
        return {"assessment": {"enough": enough, "evidence_ids": cited, "missing": out.missing}}

    def after_evidence_check(state: InvestigationState) -> str:
        if (state.get("assessment") or {}).get("enough"):
            return "diagnose"
        if _remaining_steps(state, settings) > 0 and state.get("passes", 0) < settings.investigation_max_passes:
            return "triage"
        return "escalate"

    async def diagnose(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "diagnose", state.get("steps_used", 0))
        evidence = state.get("evidence", [])
        assessment = state.get("assessment") or {}
        context = (
            f"{case_text(state)}\n\nIssue type so far: {state.get('issue_type')}\n\n"
            f"Evidence check: cited {', '.join(assessment.get('evidence_ids', [])) or 'none'}\n\n"
            f"Evidence:\n{evidence_lines(evidence, with_data=True)}"
        )
        try:
            out = await invoke_structured(
                deps.llm.get(ModelAlias.REASONING),
                DiagnosisOutput,
                [SystemMessage(content=prompts.DIAGNOSE_SYSTEM), HumanMessage(content=context)],
                config,
            )
        except StructuredOutputError as exc:
            log.warning("diagnose: unusable model output (%s)", exc)
            return {
                "diagnosis": None,
                "findings": (state.get("findings") or "") + " The diagnosis could not be written.",
            }
        known = {e["id"] for e in evidence}
        cited = [i for i in out.evidence_ids if i in known] or list(assessment.get("evidence_ids", []))
        if not cited:
            return {"diagnosis": None}

        explain = [
            SystemMessage(content=prompts.EXPLAIN_SYSTEM),
            HumanMessage(
                content=f"{context}\n\nDiagnosis: issue type {out.issue_type.value}, root cause {out.root_cause.value}, "
                f"confidence {out.confidence:.2f}, evidence {', '.join(cited)}.\n"
                f"Recommended action: {out.recommended_action}"
            ),
        ]
        pieces: list[str] = []
        tokens = MaskedTokenStream()  # a number split across chunks is still masked
        async for chunk in deps.llm.get(ModelAlias.REASONING).astream(explain, config):
            piece = chunk.content if isinstance(chunk.content, str) else ""
            if piece:
                pieces.append(piece)
                if ready := tokens.push(piece):
                    await emit(config, "token", {"text": ready})
        if rest := tokens.flush():
            await emit(config, "token", {"text": rest})
        explanation = mask_text("".join(pieces).strip()) or (
            f"Root cause {out.root_cause.value}, shown by {', '.join(cited)}."
        )
        diagnosis = {
            "id": str(uuid.uuid4()),
            "issue_type": out.issue_type.value,
            "root_cause": out.root_cause.value,
            "explanation": explanation,
            "confidence": out.confidence,
            "evidence_ids": cited,
            "recommended_action": mask_text(out.recommended_action),
        }
        await emit(config, "diagnosis", {"diagnosis": diagnosis})
        return {"diagnosis": diagnosis, "issue_type": out.issue_type.value}

    def after_diagnose(state: InvestigationState) -> str:
        return "review" if state.get("diagnosis") else "escalate"

    async def escalate(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        steps_used = state.get("steps_used", 0)
        await step_started(config, "escalate", steps_used)
        evidence = state.get("evidence", [])
        missing = (state.get("assessment") or {}).get("missing")
        lines = [
            f"No conclusion within the step budget ({steps_used - state.get('budget_base', 0)} of "
            f"{settings.investigation_max_steps} steps, {state.get('passes', 0)} pass(es)). "
            f"Issue type: {state.get('issue_type') or 'unknown'}.",
        ]
        if state.get("findings"):
            lines.append(f"Findings: {state['findings']}")
        if missing:
            lines.append(f"Still unknown: {missing}")
        lines.append("Evidence:\n" + evidence_lines(evidence))
        escalation = {"summary": mask_text("\n".join(lines)), "evidence_ids": [e["id"] for e in evidence]}
        await emit(config, "escalated", {"escalation": escalation})
        return {"escalation": escalation, "diagnosis": None}

    def review(state: InvestigationState) -> dict[str, Any]:
        """Human review. Pauses the run until the ops agent's decision is resumed in."""
        value = interrupt(
            {"investigation_id": state.get("investigation_id"), "has_diagnosis": bool(state.get("diagnosis"))}
        )
        decision = ReviewDecision.model_validate(value)
        update: dict[str, Any] = {"review": decision.model_dump(mode="json"), "last_review_id": decision.review_id}
        if decision.verdict is Verdict.REJECTED:
            update.update(
                reviewer_feedback=decision.hint,
                diagnosis=None,
                escalation=None,
                assessment=None,
                budget_base=state.get("steps_used", 0),
                passes=0,
            )
        return update

    def after_review(state: InvestigationState) -> str:
        verdict = (state.get("review") or {}).get("verdict")
        return "triage" if verdict == Verdict.REJECTED.value else "finalize"

    async def finalize(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
        await step_started(config, "finalize", state.get("steps_used", 0))
        decision = state.get("review") or {}
        diagnosis = state.get("diagnosis") or {}
        final: dict[str, Any] = {
            "verdict": decision.get("verdict"),
            "issue_type": decision.get("corrected_issue_type")
            or diagnosis.get("issue_type")
            or state.get("issue_type"),
            "root_cause": decision.get("corrected_root_cause") or diagnosis.get("root_cause"),
            "had_diagnosis": bool(diagnosis),
            "steps_used": state.get("steps_used", 0),
        }
        if deps.score is not None:
            investigation_id = state.get("investigation_id") or ""
            try:
                if diagnosis:
                    accepted = 1.0 if decision.get("verdict") == Verdict.ACCEPTED.value else 0.0
                    deps.score(investigation_id, "diagnosis_accepted", accepted, decision.get("verdict"))
                if state.get("provided_category"):
                    deps.score(investigation_id, "category_changed", float(bool(state.get("category_changed"))), None)
                deps.score(investigation_id, "steps_used", float(state.get("steps_used", 0)), None)
            except Exception:  # scoring must never fail a review
                log.warning("investigation %s: Langfuse scoring failed", investigation_id, exc_info=True)
        return {"outcome": "finalized", "final": final}

    graph = StateGraph(InvestigationState)
    graph.add_node("intake", intake)
    graph.add_node("verify", verify)
    graph.add_node("triage", triage)
    graph.add_node("route", route)
    graph.add_node("park", park)
    for issue_type in IssueType:
        graph.add_node(INVESTIGATOR_NODES[issue_type], make_investigator_node(issue_type))
    graph.add_node("evidence_check", evidence_check)
    graph.add_node("diagnose", diagnose)
    graph.add_node("escalate", escalate)
    graph.add_node("review", review)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", has_category, {"verify": "verify", "triage": "triage"})
    graph.add_conditional_edges("verify", after_verify, {"route": "route", "triage": "triage"})
    graph.add_edge("triage", "route")
    graph.add_conditional_edges(
        "route", choose_route, {"park": "park", **{name: name for name in INVESTIGATOR_NODES.values()}}
    )
    graph.add_edge("park", END)
    for name in INVESTIGATOR_NODES.values():
        graph.add_edge(name, "evidence_check")
    graph.add_conditional_edges(
        "evidence_check",
        after_evidence_check,
        {"diagnose": "diagnose", "triage": "triage", "escalate": "escalate"},
    )
    graph.add_conditional_edges("diagnose", after_diagnose, {"review": "review", "escalate": "escalate"})
    graph.add_edge("escalate", "review")
    graph.add_conditional_edges("review", after_review, {"finalize": "finalize", "triage": "triage"})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer, name="investigation")
