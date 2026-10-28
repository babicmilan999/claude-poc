"""Simulation answer keys never reach an agent, a tool or a prompt."""

import importlib
import json
import pkgutil

import pytest
from pydantic import BaseModel

import core.prompts
from core.banking import models as banking_models
from core.tools import customer_views, make_ops_tools
from core.tools.banking_customer import make_customer_tools

ANSWER_KEYS = ("expected_root_cause", "expected_outcome", "expected")


def _prompt_strings() -> list[tuple[str, str]]:
    out = []
    for info in pkgutil.iter_modules(core.prompts.__path__):
        module = importlib.import_module(f"core.prompts.{info.name}")
        for name, value in vars(module).items():
            if isinstance(value, str) and not name.startswith("__"):
                out.append((f"{info.name}.{name}", value))
    return out


@pytest.mark.parametrize(("name", "value"), _prompt_strings())
def test_prompts_have_no_answer_keys(name: str, value: str) -> None:
    for key in ANSWER_KEYS:
        assert key not in value.lower(), f"{name} mentions {key}"


def test_tools_have_no_answer_keys(banking) -> None:
    tools = make_customer_tools(banking, "11111111-1111-4111-8111-111111111111") + make_ops_tools(
        banking, "11111111-1111-4111-8111-111111111111"
    )
    for tool in tools:
        schema = tool.tool_call_schema.model_json_schema()  # type: ignore[union-attr]
        surface = tool.description + json.dumps(schema)
        for key in ANSWER_KEYS:
            assert key not in surface.lower(), tool.name


def _models(module: object) -> list[type[BaseModel]]:
    return [v for v in vars(module).values() if isinstance(v, type) and issubclass(v, BaseModel)]


def test_agent_facing_models_have_no_answer_key_fields() -> None:
    from core.signals import SignalSnapshot
    from core.triage import TriageOutput, TriageResult

    models = _models(banking_models) + _models(customer_views) + [SignalSnapshot, TriageOutput, TriageResult]
    for model in models:
        for field in model.model_fields:
            assert not field.startswith("expected"), f"{model.__name__}.{field}"
