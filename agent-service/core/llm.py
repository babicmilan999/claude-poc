"""LLM access through the LiteLLM proxy, by alias only.

The code never names a model or a provider: it asks for a role alias (`triage`,
`assistant`, `reasoning`, `simulator`, `judge`) and the LiteLLM profile maps it to a real
model. LiteLLM speaks the OpenAI API, so the chat model is LangChain's OpenAI client
pointed at the proxy, with the alias as the model name.

Every model handed out by an `LLMProvider` is wrapped in `MaskedChatModel`, which masks
card numbers and other identifiers in every message before the request leaves the
service. Tests swap the provider for `tests.fakes.FakeLLMProvider`; the masking wrapper
stays in place.
"""

import json
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from enum import StrEnum
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import BaseModel, ValidationError

from core.masking import mask_data, mask_text
from core.settings import Settings


class ModelAlias(StrEnum):
    TRIAGE = "triage"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    SIMULATOR = "simulator"
    JUDGE = "judge"


# --- masking -------------------------------------------------------------------


def _mask_content(content: str | list[Any]) -> str | list[Any]:
    if isinstance(content, str):
        return mask_text(content)
    masked: list[Any] = []
    for block in content:
        if isinstance(block, str):
            masked.append(mask_text(block))
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            masked.append({**block, "text": mask_text(block["text"])})
        else:
            masked.append(block)
    return masked


def mask_message(message: BaseMessage) -> BaseMessage:
    update: dict[str, Any] = {"content": _mask_content(message.content)}
    if isinstance(message, AIMessage) and message.tool_calls:
        update["tool_calls"] = [{**call, "args": mask_data(call["args"])} for call in message.tool_calls]
    return message.model_copy(update=update)


def mask_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    return [mask_message(m) for m in messages]


class MaskedChatModel(BaseChatModel):
    """Wraps a chat model; masks every message before it reaches the inner model."""

    inner: BaseChatModel
    alias: str
    llm_profile: str = "unknown"  # the LiteLLM profile (BaseChatModel.profile is something else)

    @property
    def _llm_type(self) -> str:
        return f"masked-{self.inner._llm_type}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"alias": self.alias, "llm_profile": self.llm_profile}

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> Runnable[LanguageModelInput, AIMessage]:
        # Let the inner model format the tools (and tool_choice) its own way, then
        # bind the same request kwargs on the wrapper so calls still go through masking.
        binding = self.inner.bind_tools(tools, **kwargs)
        return self.bind(**getattr(binding, "kwargs", {}))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self.inner._generate(mask_messages(messages), stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await self.inner._agenerate(mask_messages(messages), stop=stop, run_manager=run_manager, **kwargs)

    def _inner_streams(self) -> bool:
        return type(self.inner)._stream is not BaseChatModel._stream

    def _inner_astreams(self) -> bool:
        return type(self.inner)._astream is not BaseChatModel._astream or self._inner_streams()

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        masked = mask_messages(messages)
        if self._inner_streams():
            yield from self.inner._stream(masked, stop=stop, run_manager=run_manager, **kwargs)
            return
        result = self.inner._generate(masked, stop=stop, run_manager=run_manager, **kwargs)
        yield _as_chunk(result.generations[0])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        masked = mask_messages(messages)
        if self._inner_astreams():
            async for chunk in self.inner._astream(masked, stop=stop, run_manager=run_manager, **kwargs):
                yield chunk
            return
        result = await self.inner._agenerate(masked, stop=stop, run_manager=run_manager, **kwargs)
        yield _as_chunk(result.generations[0])


def _as_chunk(generation: ChatGeneration) -> ChatGenerationChunk:
    message = generation.message
    chunk = AIMessageChunk(
        content=message.content,
        id=message.id,
        tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
            for i, c in enumerate(getattr(message, "tool_calls", []) or [])
        ],
        usage_metadata=getattr(message, "usage_metadata", None),
        response_metadata=message.response_metadata,
    )
    return ChatGenerationChunk(message=chunk, generation_info=generation.generation_info)


# --- providers -------------------------------------------------------------------


class LLMProvider:
    """Hands out chat models by alias, always wrapped in `MaskedChatModel`."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self._cache: dict[ModelAlias, MaskedChatModel] = {}

    def _build(self, alias: ModelAlias) -> BaseChatModel:
        raise NotImplementedError

    def get(self, alias: ModelAlias | str) -> MaskedChatModel:
        alias = ModelAlias(alias)
        if alias not in self._cache:
            self._cache[alias] = MaskedChatModel(
                inner=self._build(alias),
                alias=alias.value,
                llm_profile=self.profile,
                metadata={"llm_alias": alias.value, "llm_profile": self.profile},
                tags=[f"llm_alias:{alias.value}", f"llm_profile:{self.profile}"],
            )
        return self._cache[alias]


class LiteLLMProvider(LLMProvider):
    """Chat models served by the LiteLLM proxy (OpenAI-compatible API)."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings.llm_profile)
        self._settings = settings

    def _build(self, alias: ModelAlias) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=alias.value,  # the alias is the model name LiteLLM routes on
            base_url=self._settings.litellm_base_url.rstrip("/") + "/v1",
            api_key=self._settings.litellm_master_key,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
            stream_usage=True,
        )


# --- structured output -------------------------------------------------------------


class StructuredOutputError(Exception):
    """The model did not return output matching the schema."""


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_from_content[S: BaseModel](schema: type[S], message: BaseMessage) -> S | None:
    content = message.content
    if isinstance(content, list):
        content = "".join(b if isinstance(b, str) else str(b.get("text", "")) for b in content)
    match = _JSON_OBJECT_RE.search(content or "")
    if not match:
        return None
    try:
        return schema.model_validate_json(match.group(0))
    except ValidationError:
        return None


async def invoke_structured[S: BaseModel](
    model: BaseChatModel,
    schema: type[S],
    messages: Sequence[BaseMessage],
    config: RunnableConfig | None = None,
) -> S:
    """One call with structured output (tool calling), with a JSON-in-text fallback for
    small local models that answer in plain text."""
    runnable = model.with_structured_output(schema, include_raw=True)
    result: Any = await runnable.ainvoke(list(messages), config=config)
    parsed = result.get("parsed")
    if isinstance(parsed, schema):
        return parsed
    raw = result.get("raw")
    if isinstance(raw, BaseMessage):
        fallback = _parse_from_content(schema, raw)
        if fallback is not None:
            return fallback
    raise StructuredOutputError(f"model output does not match {schema.__name__}: {result.get('parsing_error')}")
