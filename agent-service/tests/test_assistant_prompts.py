"""Assistant prompts and agent-facing models carry no simulation answer keys."""

from pydantic import BaseModel

from assistant import prompts, rules, schemas
from assistant.graph import AssistantState
from core.enums import IssueType

ANSWER_KEYS = ("expected_root_cause", "expected_outcome", "expected")


def _strings() -> list[tuple[str, str]]:
    out = [
        (name, value) for name, value in vars(prompts).items() if isinstance(value, str) and not name.startswith("__")
    ]
    out += [(f"PLAYBOOKS[{k}]", v) for k, v in prompts.PLAYBOOKS.items()]
    out += [(f"HANDOFF_OPENERS[{k}]", v) for k, v in prompts.HANDOFF_OPENERS.items()]
    return out


def test_prompts_have_no_answer_keys() -> None:
    for name, value in _strings():
        for key in ANSWER_KEYS:
            assert key not in value.lower(), f"{name} mentions {key}"


def test_every_category_has_a_playbook_and_every_rule_an_opener() -> None:
    assert set(prompts.PLAYBOOKS) == set(IssueType)
    assert set(prompts.HANDOFF_OPENERS) == {r.value for r in rules.HandoffReason}


def test_graph_state_and_api_models_have_no_answer_key_fields() -> None:
    assert not [k for k in AssistantState.__annotations__ if k.startswith("expected")]
    for value in vars(schemas).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            assert not [f for f in value.model_fields if f.startswith("expected")], value.__name__


def test_prompt_versions_are_set() -> None:
    assert prompts.PROMPT_VERSION and prompts.SUMMARY_PROMPT_VERSION
