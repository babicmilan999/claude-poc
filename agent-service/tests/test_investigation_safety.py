"""Investigation package guard rails: no answer keys in prompts, state or models; only the
`triage` and `reasoning` aliases; every model goes through the masking wrapper."""

import inspect
import pkgutil
import typing

import pytest
from pydantic import BaseModel

import investigation
from investigation import prompts, schemas, state, store

ANSWER_KEYS = ("expected_root_cause", "expected_outcome", "expected")


def _prompt_strings() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, value in vars(prompts).items():
        if name.startswith("__"):
            continue
        if isinstance(value, str):
            out.append((name, value))
        elif isinstance(value, dict):
            out += [(f"{name}[{k}]", v) for k, v in value.items() if isinstance(v, str)]
    return out


@pytest.mark.parametrize(("name", "value"), _prompt_strings())
def test_prompts_have_no_answer_keys(name: str, value: str) -> None:
    for key in ANSWER_KEYS:
        assert key not in value.lower(), f"{name} mentions {key}"


def test_graph_state_and_models_have_no_answer_key_fields() -> None:
    fields = list(typing.get_type_hints(state.InvestigationState))
    models = [
        v
        for module in (schemas, state)
        for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, BaseModel)
    ]
    for model in models:
        fields += [f"{model.__name__}.{f}" for f in model.model_fields]
    fields += [f"IntakeData.{f}" for f in store.IntakeData.__dataclass_fields__]
    for name in fields:
        assert "expected" not in name, name


def test_code_names_only_model_aliases() -> None:
    """The package asks for aliases (`ModelAlias.TRIAGE` via core triage, `REASONING`)."""
    for info in pkgutil.iter_modules(investigation.__path__):
        module = __import__(f"investigation.{info.name}", fromlist=["_"])
        source = inspect.getsource(module)
        for banned in ("ChatOpenAI", "ChatAnthropic", "claude-", "gpt-", "llama", "qwen", "ollama/"):
            assert banned not in source, f"investigation.{info.name} names {banned}"
        for alias in ("ASSISTANT", "SIMULATOR", "JUDGE"):
            assert f"ModelAlias.{alias}" not in source, f"investigation.{info.name} uses {alias}"


def test_the_sql_store_never_selects_answer_keys() -> None:
    from investigation import store_sql

    source = inspect.getsource(store_sql)
    assert "expected_root_cause" not in source and "expected_outcome" not in source
    assert all("expected" not in str(c) for c in store_sql._CASE_COLUMNS)
