"""Fake LLM for unit tests: scripted responses, including tool calls and structured output.

Usage:

    llm = FakeLLMProvider({
        "triage": [structured(TriageOutput(category="card_decline", confidence=0.9, ...))],
        "assistant": [tool_call("get_my_cards", {}), text("Your card is frozen.")],
    })
    model = llm.get("assistant")          # MaskedChatModel wrapping a FakeChatModel
    llm.fake("assistant").calls           # what the model was sent (already masked)

A scripted item can be an `AIMessage`, a plain string (a text answer), or a callable
that receives the masked messages and returns either of those.
"""

import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

from core.llm import LLMProvider, ModelAlias

Scripted = AIMessage | str | Callable[[list[BaseMessage]], AIMessage | str]


def text(content: str) -> AIMessage:
    return AIMessage(content=content)


def tool_call(name: str, args: Mapping[str, Any] | None = None, call_id: str | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": dict(args or {}), "id": call_id or f"call_{uuid.uuid4().hex[:8]}"}],
    )


def tool_calls(*calls: tuple[str, Mapping[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": dict(args), "id": f"call_{uuid.uuid4().hex[:8]}"} for name, args in calls],
    )


def structured(value: BaseModel) -> AIMessage:
    """The tool call a model makes when asked for structured output of this schema."""
    return tool_call(type(value).__name__, value.model_dump(mode="json"))


def structured_as_text(value: BaseModel) -> AIMessage:
    """Structured output written as JSON in plain text (what small local models often do)."""
    return text(f"Here you go:\n{json.dumps(value.model_dump(mode='json'))}")


@dataclass
class FakeCall:
    messages: list[BaseMessage]
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.kwargs.get("tools") or []]

    @property
    def text(self) -> str:
        return "\n".join(str(m.content) for m in self.messages)


class FakeChatModel(BaseChatModel):
    responses: list[Any] = Field(default_factory=list)
    calls: list[FakeCall] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any) -> Any:
        formatted = [convert_to_openai_tool(t) for t in tools]
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=formatted, **kwargs)

    def _next(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> AIMessage:
        self.calls.append(FakeCall(messages=list(messages), kwargs=dict(kwargs)))
        if not self.responses:
            raise AssertionError(f"FakeChatModel script exhausted after {len(self.calls) - 1} call(s)")
        item = self.responses.pop(0)
        if callable(item) and not isinstance(item, BaseMessage):
            item = item(list(messages))
        if isinstance(item, str):
            item = AIMessage(content=item)
        if not isinstance(item, AIMessage):
            raise TypeError(f"unsupported scripted response: {item!r}")
        return item.model_copy(update={"id": item.id or f"fake-{uuid.uuid4().hex[:8]}"})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages, kwargs))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._next(messages, kwargs)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id=message.id,
                    tool_call_chunks=[
                        {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                        for i, c in enumerate(message.tool_calls)
                    ],
                )
            )
            return
        content = message.content if isinstance(message.content, str) else str(message.content)
        tokens = [t for t in content.split(" ")]
        for i, token in enumerate(tokens):
            piece = token if i == len(tokens) - 1 else token + " "
            if run_manager:
                run_manager.on_llm_new_token(piece)
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece, id=message.id))


class FakeLLMProvider(LLMProvider):
    """`LLMProvider` whose aliases are served by scripted `FakeChatModel`s."""

    def __init__(self, scripts: Mapping[str, list[Scripted]] | None = None) -> None:
        super().__init__(profile="fake")
        self._fakes: dict[ModelAlias, FakeChatModel] = {
            alias: FakeChatModel(responses=list((scripts or {}).get(alias.value, []))) for alias in ModelAlias
        }

    def _build(self, alias: ModelAlias) -> BaseChatModel:
        return self._fakes[alias]

    def fake(self, alias: ModelAlias | str) -> FakeChatModel:
        return self._fakes[ModelAlias(alias)]

    def script(self, alias: ModelAlias | str, *responses: Scripted) -> None:
        self.fake(alias).responses.extend(responses)
