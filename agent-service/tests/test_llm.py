"""LLM access by alias, structured output, and the fake LLM itself."""

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, SecretStr

from core.llm import LiteLLMProvider, MaskedChatModel, ModelAlias, StructuredOutputError, invoke_structured
from core.settings import Settings
from tests.fakes import FakeLLMProvider, structured, structured_as_text, text, tool_call


class Verdict(BaseModel):
    """A test schema."""

    label: str
    score: float


def test_aliases_are_the_five_roles() -> None:
    assert [a.value for a in ModelAlias] == ["triage", "assistant", "reasoning", "simulator", "judge"]


def test_provider_wraps_every_alias_with_masking_and_metadata() -> None:
    llm = FakeLLMProvider()
    for alias in ModelAlias:
        model = llm.get(alias)
        assert isinstance(model, MaskedChatModel)
        assert {"llm_alias": alias.value, "llm_profile": "fake"}.items() <= (model.metadata or {}).items()
        assert llm.get(alias.value) is model  # cached
    with pytest.raises(ValueError):
        llm.get("gpt-4o")  # only aliases, never model names


def test_litellm_provider_points_at_the_proxy_with_the_alias_as_model() -> None:
    settings = Settings(
        litellm_base_url="http://litellm:4000", litellm_master_key=SecretStr("sk-test"), llm_profile="mixed"
    )
    model = LiteLLMProvider(settings).get(ModelAlias.REASONING)
    inner = model.inner
    assert inner.model_name == "reasoning"  # type: ignore[attr-defined]
    assert str(inner.openai_api_base) == "http://litellm:4000/v1"  # type: ignore[attr-defined]
    assert {"llm_alias": "reasoning", "llm_profile": "mixed"}.items() <= (model.metadata or {}).items()


async def test_invoke_structured_from_tool_call() -> None:
    llm = FakeLLMProvider({"triage": [structured(Verdict(label="x", score=0.5))]})
    out = await invoke_structured(llm.get("triage"), Verdict, [HumanMessage(content="hi")])
    assert out == Verdict(label="x", score=0.5)
    call = llm.fake("triage").calls[0]
    assert call.tool_names == ["Verdict"]


async def test_invoke_structured_falls_back_to_json_in_text() -> None:
    llm = FakeLLMProvider({"triage": [structured_as_text(Verdict(label="y", score=1.0))]})
    out = await invoke_structured(llm.get("triage"), Verdict, [HumanMessage(content="hi")])
    assert out.label == "y"


async def test_invoke_structured_raises_on_garbage() -> None:
    llm = FakeLLMProvider({"triage": [text("I am not sure.")]})
    with pytest.raises(StructuredOutputError):
        await invoke_structured(llm.get("triage"), Verdict, [HumanMessage(content="hi")])


async def test_bind_tools_reaches_the_fake_and_tool_calls_come_back() -> None:
    from langchain_core.tools import tool

    @tool
    def ping(x: int) -> int:
        """Ping."""
        return x

    llm = FakeLLMProvider({"reasoning": [tool_call("ping", {"x": 1}, call_id="c1")]})
    result = await llm.get("reasoning").bind_tools([ping]).ainvoke([HumanMessage(content="go")])
    assert result.tool_calls[0]["name"] == "ping" and result.tool_calls[0]["args"] == {"x": 1}
    assert llm.fake("reasoning").calls[0].tool_names == ["ping"]


async def test_fake_raises_when_script_is_exhausted() -> None:
    llm = FakeLLMProvider()
    with pytest.raises(AssertionError, match="exhausted"):
        await llm.get("judge").ainvoke([HumanMessage(content="hi")])
