"""The assistant graph (architecture section 4): one run per customer message.

    load_context ─┬─ small_talk ──────────────────────────────────────────────► END
                  └─ input_guard ─┬─ deflect ─────────────────────────────────► END
                                  └─ triage ── handoff_check ─┬─ handoff ─────► END
                                                              └─ answer_agent ◄──┐
                                                                  │              │ rejected once
                                                                output_guard ────┤
                                                                  │ passed       │ rejected twice
                                                                respond ► END    └► handoff

The conversation is a LangGraph thread (`thread_id = conversation_id`): the messages and
the evidence gathered so far are kept between runs. Everything that must be predictable is
code: the signal snapshot, the handoff rules, running the tools, masking (every model comes
from `LLMProvider`, wrapped in `MaskedChatModel`), and saving messages and the case.

Nodes that talk to the customer emit chat events with LangGraph's `custom` stream mode
(`get_stream_writer()`); `assistant.service` adds the event ids and fans them out to the
SSE response and watch mode. The approved answer is streamed in `respond`, after the output
guard, so the customer never sees an answer the guard rejected.
"""

import logging
import operator
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from assistant import prompts
from assistant import schemas as ev
from assistant.rules import HandoffDecision, HandoffReason, evaluate_handoff_rules, said_not_helpful
from assistant.store import ConversationStore, NewHandoffCase
from core.banking import BankingClient
from core.enums import EvidenceOrigin, IssueType, MessageRole
from core.guardrails import InputGuardResult, Source, check_input, check_output, refusal_message
from core.kb import KnowledgeBaseSearch
from core.llm import LLMProvider, ModelAlias
from core.masking import mask_data
from core.signals import SignalSnapshot, build_signal_snapshot
from core.tools import make_assistant_tools
from core.tools.common import Clock, utc_now
from core.triage import TriageResult, triage

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4  # step budget of the answer agent: a customer is waiting
MAX_GUARD_REJECTIONS = 2  # the output guard rejected two answers -> hand off
HISTORY_MESSAGES = 12  # earlier messages the answer agent sees
TRIAGE_CONTEXT_MESSAGES = 3  # recent customer messages triage reads (follow-ups stay on topic)
EARLIER_EVIDENCE = 12  # evidence from earlier turns offered to the output guard as sources
MAX_DISPLAY_ARTICLES = 3

KB_TOOL = "search_knowledge_base"
TOOL_TITLES = {
    "get_my_cards": "Your cards",
    "get_my_recent_declines": "Your recent declined payments",
    "get_my_card_controls": "Your card controls",
    "get_my_scheduled_payments": "Your scheduled payments",
    "get_my_direct_debits": "Your Direct Debits",
    "get_my_balance": "Your balance",
}

_GREETING_RE = re.compile(r"^\s*(?:hi|hello|hey|hiya|good\s+(?:morning|afternoon|evening))(?:\s+there)?[\s!.,]*$", re.I)
_THANKS_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[\s,]*)?(?:great[\s,]*|perfect[\s,]*)?(?:thanks|thank\s+you(?:\s+so\s+much)?|cheers|ta)"
    r"(?:[\s,]+(?:that'?s\s+all|bye))?[\s!.,]*$",
    re.I,
)

Result = Literal["answered", "deflected", "handed_off", "small_talk"]


class AssistantState(TypedDict, total=False):
    # --- kept for the whole conversation ---
    conversation_id: str
    customer_id: str
    messages: Annotated[list[AnyMessage], add_messages]  # customer messages and sent replies
    evidence: Annotated[list[dict[str, Any]], operator.add]  # every tool result (contract `Evidence`)
    last_answer_id: int | None  # the latest answer sent (message id); "that didn't help" refers to it
    not_helpful_answers: list[int]  # answers the customer said in words did not help
    down_rated: list[int]  # assistant messages rated 👎 so far; set by the caller on every run
    # --- this message only (reset by load_context) ---
    small_talk: str | None
    snapshot: dict[str, Any] | None
    input_guard: dict[str, Any] | None
    triage: dict[str, Any] | None
    handoff: dict[str, Any] | None
    draft: str | None
    guard_sources: list[dict[str, Any]]
    display_sources: list[dict[str, Any]]
    guard_attempts: int
    guard_problems: list[str]
    result: Result | None
    reply_message_id: int | None
    case_id: str | None


@dataclass
class AssistantDeps:
    llm: LLMProvider
    banking: BankingClient
    kb: KnowledgeBaseSearch | None
    store: ConversationStore
    triage_threshold: float
    clock: Clock = utc_now


# --- helpers ---------------------------------------------------------------------------


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    parts = [b if isinstance(b, str) else str(b.get("text", "")) for b in content if isinstance(b, str | dict)]
    return "".join(parts).strip()


def _last_customer_message(state: AssistantState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return _text(message)
    return ""


def _customer_messages(state: AssistantState) -> list[str]:
    return [_text(m) for m in state.get("messages", []) if isinstance(m, HumanMessage)]


def _not_helpful_answers(state: AssistantState, message: str) -> list[int]:
    """The answers the customer has said did not help, with this message. Words only count
    once there is an answer to refer to ("still declined" in the first message describes
    the problem), and once per answer."""
    said = list(state.get("not_helpful_answers", []))
    last = state.get("last_answer_id")
    if last is not None and last not in said and said_not_helpful(message):
        said.append(last)
    return said


def _not_helpful_count(state: AssistantState) -> int:
    """Answers that did not help, by 👎 or in words; both about the same answer count once."""
    return len(set(state.get("not_helpful_answers", [])) | set(state.get("down_rated", [])))


def _snapshot(state: AssistantState) -> SignalSnapshot | None:
    data = state.get("snapshot")
    return SignalSnapshot.model_validate(data) if data else None


def _triage(state: AssistantState) -> TriageResult | None:
    data = state.get("triage")
    return TriageResult.model_validate(data) if data else None


def _category(state: AssistantState) -> IssueType:
    result = _triage(state)
    return result.category if result else IssueType.OTHER


def _conversation_uuid(state: AssistantState) -> uuid.UUID:
    return uuid.UUID(state["conversation_id"])


def _tokens(text: str) -> list[str]:
    return re.findall(r"\s*\S+\s*", text) or [text]


def _summarise_result(tool: str, data: Any) -> str:
    title = TOOL_TITLES.get(tool, "Help articles" if tool == KB_TOOL else tool)
    if isinstance(data, list):
        return f"{title}: {len(data)} result{'s' if len(data) != 1 else ''}"
    if isinstance(data, dict) and data.get("error"):
        return f"{title}: {data['error']}"
    return title


def _sources_from_evidence(items: Sequence[dict[str, Any]]) -> list[Source]:
    out: list[Source] = []
    for item in items:
        data = item.get("data")
        if item.get("tool") == KB_TOOL and isinstance(data, list):
            out.extend(
                Source(kind="article", name=str(hit.get("title", "")), content=hit.get("text", ""))
                for hit in data
                if isinstance(hit, dict)
            )
        elif item.get("tool") != "signal_snapshot":
            out.append(Source(kind="tool", name=str(item.get("tool")), content=data))
    return out


def _display_sources(answer: str, evidence: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the app shows under the answer: the articles it cites (or, if it names none,
    the ones it read) and the account data it looked at."""
    articles: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    for item in evidence:
        data = item.get("data")
        if item.get("tool") == KB_TOOL and isinstance(data, list):
            for hit in data:
                if isinstance(hit, dict) and hit.get("article_id"):
                    articles.setdefault(
                        str(hit["article_id"]),
                        {
                            "kind": "article",
                            "title": str(hit.get("title", hit["article_id"])),
                            "article_id": str(hit["article_id"]),
                            "url": str(hit.get("url", "")),
                        },
                    )
        elif item.get("tool") in TOOL_TITLES and not (isinstance(data, dict) and data.get("error")):
            name = str(item["tool"])
            tools.setdefault(name, {"kind": "account_data", "title": TOOL_TITLES[name], "tool": name})
    lowered = answer.lower()
    cited = [a for a in articles.values() if a["title"].lower() in lowered or a["article_id"] in lowered]
    chosen = cited or list(articles.values())
    return chosen[:MAX_DISPLAY_ARTICLES] + list(tools.values())


def _transcript(state: AssistantState) -> str:
    lines = []
    for message in state.get("messages", []):
        if isinstance(message, HumanMessage):
            lines.append(f"Customer: {_text(message)}")
        elif isinstance(message, AIMessage):
            lines.append(f"Assistant: {_text(message)}")
    return "\n".join(lines)


# --- the graph -----------------------------------------------------------------------


def build_assistant_graph(
    deps: AssistantDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def load_context(state: AssistantState) -> dict[str, Any]:
        message = _last_customer_message(state)
        reset: dict[str, Any] = {
            "small_talk": None,
            "snapshot": None,
            "input_guard": None,
            "triage": None,
            "handoff": None,
            "draft": None,
            "guard_sources": [],
            "display_sources": [],
            "guard_attempts": 0,
            "guard_problems": [],
            "result": None,
            "reply_message_id": None,
            "case_id": None,
        }
        if _GREETING_RE.match(message):
            return {**reset, "small_talk": prompts.GREETING_REPLY}
        if _THANKS_RE.match(message):
            return {**reset, "small_talk": prompts.THANKS_REPLY}
        snapshot = await build_signal_snapshot(deps.banking, state["customer_id"], clock=deps.clock)
        return {
            **reset,
            "snapshot": snapshot.model_dump(mode="json"),
            "not_helpful_answers": _not_helpful_answers(state, message),
        }

    async def small_talk(state: AssistantState) -> dict[str, Any]:
        reply = state.get("small_talk") or prompts.GREETING_REPLY
        record = await deps.store.add_message(_conversation_uuid(state), MessageRole.ASSISTANT, reply)
        get_stream_writer()(ev.message_created(record))
        return {"messages": [AIMessage(content=reply)], "result": "small_talk", "reply_message_id": record.id}

    async def input_guard(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        result = await check_input(deps.llm, _last_customer_message(state), config=config)
        return {"input_guard": result.model_dump(mode="json")}

    async def deflect(state: AssistantState) -> dict[str, Any]:
        guard = InputGuardResult.model_validate(state["input_guard"])
        reply = refusal_message(guard)
        record = await deps.store.add_message(_conversation_uuid(state), MessageRole.ASSISTANT, reply)
        get_stream_writer()(ev.message_created(record))
        return {"messages": [AIMessage(content=reply)], "result": "deflected", "reply_message_id": record.id}

    async def triage_node(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        text = "\n".join(_customer_messages(state)[-TRIAGE_CONTEXT_MESSAGES:])
        result = await triage(deps.llm, text, _snapshot(state), threshold=deps.triage_threshold, config=config)
        await deps.store.update_conversation(_conversation_uuid(state), category=result.category)
        return {"triage": result.model_dump(mode="json")}

    async def handoff_check(state: AssistantState) -> dict[str, Any]:
        result = _triage(state)
        assert result is not None
        decision = evaluate_handoff_rules(
            _last_customer_message(state),
            result,
            _snapshot(state),
            not_helpful_count=_not_helpful_count(state),
        )
        return {"handoff": decision.model_dump(mode="json") if decision.handoff else None}

    async def answer_agent(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        snapshot = _snapshot(state)
        tools: list[BaseTool] = make_assistant_tools(deps.banking, state["customer_id"], deps.kb, clock=deps.clock)
        by_name = {t.name: t for t in tools}
        model = deps.llm.get(ModelAlias.ASSISTANT).bind_tools(tools)

        facts = snapshot.to_prompt(audience="customer") if snapshot else "Account facts: not available."
        system = prompts.answer_system_prompt(_category(state), facts)
        if state.get("guard_problems"):
            problems = "\n".join(f"- {p}" for p in state["guard_problems"])
            system += "\n" + prompts.RETRY_NOTE.format(problems=problems)
        history = [m for m in state.get("messages", []) if isinstance(m, HumanMessage | AIMessage)]
        messages: list[BaseMessage] = [SystemMessage(content=system), *history[-HISTORY_MESSAGES:]]

        earlier = list(state.get("evidence", []))
        new_evidence: list[dict[str, Any]] = []
        draft = ""
        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            ai = await model.ainvoke(messages, config=config)
            assert isinstance(ai, AIMessage)
            messages.append(ai)
            if not ai.tool_calls or round_no == MAX_TOOL_ROUNDS:
                draft = _text(ai)
                break
            for call in ai.tool_calls:
                tool_message = await _run_tool(by_name, call, config)
                messages.append(tool_message)
                if tool_message.status == "error":
                    continue
                new_evidence.append(
                    {
                        "id": f"a-ev-{len(earlier) + len(new_evidence) + 1}",
                        "origin": EvidenceOrigin.ASSISTANT.value,
                        "tool": call["name"],
                        "args": mask_data(dict(call.get("args") or {})),
                        "summary": _summarise_result(call["name"], tool_message.artifact),
                        "data": tool_message.artifact,
                        "created_at": ev.timestamp(deps.clock()),
                    }
                )

        guard_sources: list[Source] = []
        if snapshot is not None:
            guard_sources.append(Source(kind="tool", name="account facts", content=snapshot.customer_safe()))
        guard_sources += _sources_from_evidence(earlier[-EARLIER_EVIDENCE:] + new_evidence)
        return {
            "draft": draft,
            "evidence": new_evidence,
            "guard_sources": [s.model_dump(mode="json") for s in guard_sources],
            "display_sources": _display_sources(draft, new_evidence or earlier[-EARLIER_EVIDENCE:]),
        }

    async def output_guard(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        draft = state.get("draft") or ""
        attempts = state.get("guard_attempts", 0)
        if draft:
            sources = [Source.model_validate(s) for s in state.get("guard_sources", [])]
            result = await check_output(deps.llm, draft, sources, config=config)
            if result.passed:
                return {"guard_problems": []}
            problems = [*result.reasons, *(f"unsupported: {c}" for c in result.unsupported_claims)]
        else:
            problems = ["no answer was written within the step budget"]
        attempts += 1
        update: dict[str, Any] = {"guard_attempts": attempts, "guard_problems": problems}
        if attempts >= MAX_GUARD_REJECTIONS:
            decision = HandoffDecision()
            decision.add(HandoffReason.REPEATED_FAILURE, "the output guard rejected two answers")
            update["handoff"] = decision.model_dump(mode="json")
        return update

    async def respond(state: AssistantState) -> dict[str, Any]:
        answer = state.get("draft") or ""
        shown = list(state.get("display_sources", []))
        record = await deps.store.add_message(_conversation_uuid(state), MessageRole.ASSISTANT, answer, shown)
        write = get_stream_writer()
        for piece in _tokens(answer):
            write(ev.token(record.id, piece))
        write(ev.sources(record.id, shown))
        write(ev.message_completed(record))
        return {
            "messages": [AIMessage(content=answer)],
            "result": "answered",
            "reply_message_id": record.id,
            "last_answer_id": record.id,
        }

    async def handoff(state: AssistantState, config: RunnableConfig) -> dict[str, Any]:
        decision = HandoffDecision.model_validate(state.get("handoff") or {})
        primary = decision.primary or HandoffReason.LOW_CONFIDENCE
        snapshot = _snapshot(state)
        result = _triage(state)
        category = result.category if result else IssueType.OTHER
        summary = await _handoff_summary(deps.llm, state, decision, result, snapshot, config)

        evidence = list(state.get("evidence", []))
        if snapshot is not None:
            evidence.insert(
                0,
                {
                    "id": "a-ev-0",
                    "origin": EvidenceOrigin.ASSISTANT.value,
                    "tool": "signal_snapshot",
                    "args": {},
                    "summary": "Account signals when the customer asked",
                    "data": mask_data(snapshot.model_dump(mode="json")),
                    "created_at": ev.timestamp(snapshot.taken_at),
                },
            )
        notice = f"{prompts.HANDOFF_OPENERS[primary.value]} {prompts.HANDOFF_NOTICE}"
        created = await deps.store.create_handoff_case(
            NewHandoffCase(
                conversation_id=_conversation_uuid(state),
                customer_id=uuid.UUID(state["customer_id"]),
                customer_name=None,
                category=category,
                complaint="\n".join(_customer_messages(state)),
                summary=summary,
                evidence=evidence,
                notice=notice,
            )
        )
        write = get_stream_writer()
        write(ev.message_created(created.notice))
        write(ev.handoff(created.case_id, prompts.HANDOFF_NOTICE))
        write(ev.conversation_status(created.conversation.status, created.conversation.outcome))
        return {
            "messages": [AIMessage(content=notice)],
            "result": "handed_off",
            "reply_message_id": created.notice.id,
            "case_id": str(created.case_id),
        }

    def after_load(state: AssistantState) -> str:
        return "small_talk" if state.get("small_talk") else "input_guard"

    def after_input_guard(state: AssistantState) -> str:
        guard = InputGuardResult.model_validate(state["input_guard"])
        return "triage" if guard.allowed else "deflect"

    def after_handoff_check(state: AssistantState) -> str:
        return "handoff" if state.get("handoff") else "answer_agent"

    def after_output_guard(state: AssistantState) -> str:
        if not state.get("guard_problems"):
            return "respond"
        return "handoff" if state.get("guard_attempts", 0) >= MAX_GUARD_REJECTIONS else "answer_agent"

    graph = StateGraph(AssistantState)
    graph.add_node("load_context", load_context)
    graph.add_node("small_talk", small_talk)
    graph.add_node("input_guard", input_guard)
    graph.add_node("deflect", deflect)
    graph.add_node("triage", triage_node)
    graph.add_node("handoff_check", handoff_check)
    graph.add_node("answer_agent", answer_agent)
    graph.add_node("output_guard", output_guard)
    graph.add_node("respond", respond)
    graph.add_node("handoff", handoff)

    graph.add_edge(START, "load_context")
    graph.add_conditional_edges("load_context", after_load, ["small_talk", "input_guard"])
    graph.add_conditional_edges("input_guard", after_input_guard, ["triage", "deflect"])
    graph.add_edge("triage", "handoff_check")
    graph.add_conditional_edges("handoff_check", after_handoff_check, ["handoff", "answer_agent"])
    graph.add_edge("answer_agent", "output_guard")
    graph.add_conditional_edges("output_guard", after_output_guard, ["respond", "answer_agent", "handoff"])
    for terminal in ("small_talk", "deflect", "respond", "handoff"):
        graph.add_edge(terminal, END)
    return graph.compile(checkpointer=checkpointer, name="assistant")


async def _run_tool(by_name: dict[str, BaseTool], call: Any, config: RunnableConfig) -> ToolMessage:
    tool = by_name.get(call["name"])
    if tool is None:
        return ToolMessage(
            content=f"Error: there is no tool named {call['name']}.", tool_call_id=call["id"], status="error"
        )
    try:
        result = await tool.ainvoke({**call, "type": "tool_call"}, config=config)
    except Exception as exc:  # invalid arguments (for example a made-up customer_id)
        log.info("assistant tool %s rejected: %s", call["name"], exc)
        return ToolMessage(
            content=f"Error: invalid arguments for {call['name']}. Use only the documented arguments.",
            tool_call_id=call["id"],
            status="error",
        )
    if not isinstance(result, ToolMessage):
        return ToolMessage(content=str(result), tool_call_id=call["id"])
    return result


async def _handoff_summary(
    llm: LLMProvider,
    state: AssistantState,
    decision: HandoffDecision,
    result: TriageResult | None,
    snapshot: SignalSnapshot | None,
    config: RunnableConfig,
) -> str:
    """A short note for the ops team (`assistant` alias), with a code fallback."""
    category = result.category if result else IssueType.OTHER
    reasons = "; ".join(decision.details) or "handed off"
    messages = [
        SystemMessage(content=prompts.SUMMARY_SYSTEM),
        HumanMessage(
            content=prompts.SUMMARY_USER.format(
                category=category.value,
                confidence=result.confidence if result else 0.0,
                reasons=reasons,
                transcript=_transcript(state),
                facts=snapshot.to_prompt(audience="ops") if snapshot else "Signal snapshot: not available.",
            )
        ),
    ]
    try:
        summary = _text(await llm.get(ModelAlias.ASSISTANT).ainvoke(messages, config=config))
    except Exception as exc:
        log.warning("handoff summary failed (%s); using the fallback", exc)
        summary = ""
    if summary:
        return summary
    first = next(iter(_customer_messages(state)), "")
    return f"Customer wrote: {first!r}. Category {category.value}. Handed off because {reasons}."
