"""Helpers for the assistant tests: scripted model outputs, a service wired with fakes,
SSE parsing and contract validators."""

import json
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from assistant.graph import AssistantDeps, build_assistant_graph
from assistant.service import AssistantService
from assistant.store import InMemoryConversationStore
from core.banking import BankingClient
from core.enums import IssueType
from core.guardrails.input_guard import InputGuardOutput, InputVerdict
from core.guardrails.output_guard import OutputGuardOutput
from core.kb import KnowledgeBaseSearch
from core.tracing import Tracing
from core.triage import TriageOutput
from tests.banking_mock import AGENT_URI, NOW, contract_registry, schema_validator
from tests.fakes import FakeLLMProvider, structured

_REGISTRY = contract_registry()


def validator(name: str) -> Draft202012Validator:
    return schema_validator(AGENT_URI, name, registry=_REGISTRY)


def validate_events(events: list[dict[str, Any]]) -> None:
    chat_event = validator("ChatEvent")
    for event in events:
        errors = list(chat_event.iter_errors(event))
        assert not errors, f"{event['type']} does not match ChatEvent: {[e.message for e in errors[:3]]}"


def input_ok() -> AIMessage:
    return structured(InputGuardOutput(verdict=InputVerdict.OK, reason="payments question"))


def classified(category: IssueType, confidence: float = 0.95) -> AIMessage:
    return structured(TriageOutput(category=category, confidence=confidence, reasoning="facts"))


def judge(passed: bool = True, claims: list[str] | None = None) -> AIMessage:
    return structured(
        OutputGuardOutput(
            grounded=passed,
            unsupported_claims=claims or ([] if passed else ["made-up claim"]),
            financial_advice=False,
            internal_details=False,
            reason="all backed" if passed else "a claim has no source",
        )
    )


CARD_ANSWER = (
    "Your card ending 4242 is frozen in the app, so the payments at Tesco were declined. "
    'You can unfreeze it in the app (see "Why was my card declined?").'
)


class Harness:
    """An `AssistantService` with the fake LLM, the contract banking mock, the fixture
    knowledge base, an in-memory store and LangGraph's in-memory checkpointer."""

    def __init__(self, llm: FakeLLMProvider, banking: BankingClient, kb: KnowledgeBaseSearch | None) -> None:
        self.llm = llm
        self.store = InMemoryConversationStore(clock=lambda: NOW)
        self.checkpointer = InMemorySaver()
        deps = AssistantDeps(llm=llm, banking=banking, kb=kb, store=self.store, triage_threshold=0.6, clock=lambda: NOW)
        self.graph = build_assistant_graph(deps, self.checkpointer)
        self.service = AssistantService(
            graph=self.graph,
            store=self.store,
            banking=banking,
            tracing=Tracing(client=None, profile="test"),
            clock=lambda: NOW,
        )

    def script(self, alias: str, *responses: Any) -> None:
        self.llm.script(alias, *responses)

    async def state(self, conversation_id: Any) -> dict[str, Any]:
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": str(conversation_id)}})
        return dict(snapshot.values)

    def all_llm_text(self) -> str:
        parts = []
        for alias in ("triage", "assistant", "reasoning", "simulator", "judge"):
            for call in self.llm.fake(alias).calls:
                parts.append(call.text)
                parts.append(json.dumps(call.kwargs, default=str))
        return "\n".join(parts)


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def answer_text(events: list[dict[str, Any]]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "token")


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """(event name, data) for each SSE message; comments (pings) are skipped."""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            out.append((name or "message", json.loads("\n".join(data))))
    return out
