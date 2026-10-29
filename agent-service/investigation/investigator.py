"""Investigator subgraph (architecture section 6): a ReAct loop with a playbook prompt
and the restricted ops toolset of one issue type.

    agent (LLM picks the next tools) ──tool calls──▶ tools (execute, record evidence)
      ▲                                                 │
      └─────────────────────────────────────────────────┘
    agent ──no more calls / step budget spent──▶ END (findings)

A step is one agent LLM turn. The agent never sees a tool outside its toolset: the tools
are bound per issue type, and a call to any other name is answered with an error
without running anything. The tools are bound to the case's customer, so the model
never supplies a customer id.
"""

import json
import operator
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from core.enums import EvidenceOrigin, IssueType
from core.llm import LLMProvider, ModelAlias
from investigation import prompts
from investigation.common import emit, make_evidence, next_evidence_id

ToolsFor = Callable[[str, IssueType], list[BaseTool]]

NO_FINDINGS = "The investigator stopped without findings (step budget spent)."


class InvestigatorState(TypedDict, total=False):
    customer_id: str
    issue_type: str
    briefing: str
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: Annotated[list[dict[str, Any]], operator.add]
    taken_ids: list[str]
    steps: int
    budget: int
    done: bool


def build_investigator(issue_type: IssueType, *, llm: LLMProvider, tools_for: ToolsFor) -> CompiledStateGraph:
    playbook = prompts.PLAYBOOKS[issue_type.value]

    async def agent(state: InvestigatorState, config: RunnableConfig) -> dict[str, Any]:
        steps = state.get("steps", 0)
        budget = state.get("budget", 0)
        if steps >= budget:
            return {"done": True}
        tools = tools_for(state["customer_id"], issue_type)
        model = llm.get(ModelAlias.REASONING).bind_tools(tools)
        messages: list[AnyMessage] = [
            SystemMessage(content=playbook),
            HumanMessage(content=state["briefing"]),
            *state.get("messages", []),
        ]
        if steps == budget - 1:
            messages.append(HumanMessage(content="This is your last step: write your findings now."))
        reply = await model.ainvoke(messages, config)
        return {"messages": [reply], "steps": steps + 1}

    def after_agent(state: InvestigatorState) -> str:
        if state.get("done"):
            return END
        last = state["messages"][-1] if state.get("messages") else None
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    async def run_tools(state: InvestigatorState, config: RunnableConfig) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        by_name = {t.name: t for t in tools_for(state["customer_id"], issue_type)}
        taken = list(state.get("taken_ids", []))
        results: list[ToolMessage] = []
        evidence: list[dict[str, Any]] = []
        for call in last.tool_calls:
            call_id = call.get("id") or f"call-{len(taken)}"
            name = call["name"]
            args = dict(call.get("args") or {})
            await emit(config, "tool_call", {"call_id": call_id, "tool": name, "args": args})
            tool = by_name.get(name)
            if tool is None:
                results.append(
                    ToolMessage(
                        content=f"Error: {name} is not available to this investigator. Use one of: {', '.join(by_name)}.",
                        tool_call_id=call_id,
                        name=name,
                        status="error",
                    )
                )
                continue
            try:
                message = await tool.ainvoke({**call, "id": call_id, "type": "tool_call"}, config)
            except Exception as exc:  # bad arguments: tell the model, record nothing
                results.append(ToolMessage(content=f"Error: {exc}", tool_call_id=call_id, name=name, status="error"))
                continue
            artifact = getattr(message, "artifact", None)
            if getattr(message, "status", "success") == "error" or artifact is None:
                results.append(
                    ToolMessage(content=str(message.content), tool_call_id=call_id, name=name, status="error")
                )
                continue
            evidence_id = next_evidence_id(taken)
            taken.append(evidence_id)
            item = make_evidence(
                evidence_id=evidence_id, tool=name, args=args, data=artifact, origin=EvidenceOrigin.INVESTIGATION
            )
            evidence.append(item)
            await emit(config, "evidence", {"call_id": call_id, "evidence": item})
            results.append(
                ToolMessage(
                    content=f"[evidence {evidence_id}]\n{json.dumps(item['data'], ensure_ascii=False, default=str)}",
                    tool_call_id=call_id,
                    name=name,
                )
            )
        return {"messages": results, "evidence": evidence, "taken_ids": taken}

    graph = StateGraph(InvestigatorState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(name=f"{issue_type.value}_investigator")


def findings_of(state: InvestigatorState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage) and not message.tool_calls:
            content = message.content if isinstance(message.content, str) else str(message.content)
            if content.strip():
                return content.strip()
        if isinstance(message, AIMessage):
            break
    return NO_FINDINGS
