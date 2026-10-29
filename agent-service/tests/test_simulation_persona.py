"""The simulated customer: its prompt (persona, knowledge, no answer keys), the `simulator`
alias only, and how its structured turns are read."""

import uuid

import pytest
from pydantic import BaseModel

from simulation import persona
from simulation.persona import ChatLine, CustomerTurn, CustomerView, PersonaSpec, SimulatedCustomer
from tests.fakes import FakeLLMProvider, structured, text

ANSWER_KEYS = ("expected_root_cause", "expected_outcome", "expected", "root cause", "self_serve", "needs_human")


def view(**persona_fields: object) -> CustomerView:
    fields = {"knows": ["the card ending 4242 was declined twice today at Tesco"], **persona_fields}
    return CustomerView(
        customer_id=uuid.uuid4(),
        customer_name="Alice Example",
        persona=PersonaSpec.model_validate(fields),
        complaint="My card was declined at Tesco.",
    )


def test_system_prompt_carries_the_persona() -> None:
    prompt = persona.system_prompt(
        view(vagueness="high", patience="low", tone="anxious", phrasing=["my card keeps getting declined"])
    )
    assert "Alice" in prompt and "Example" not in prompt  # first name only
    assert "the card ending 4242 was declined twice today at Tesco" in prompt
    assert "my card keeps getting declined" in prompt
    assert "anxious" in prompt and "vague" in prompt and "impatient" in prompt


def test_system_prompt_masks_what_the_customer_knows() -> None:
    prompt = persona.system_prompt(view(knows=["my card 4111 1111 1111 1111 was declined"]))
    assert "4111 1111 1111 1111" not in prompt and "[CARD ****1111]" in prompt


def _prompt_strings() -> list[tuple[str, str]]:
    out = [
        (name, value) for name, value in vars(persona).items() if isinstance(value, str) and not name.startswith("__")
    ]
    for name in ("_VAGUENESS", "_PATIENCE"):
        out += [(f"{name}[{k}]", v) for k, v in getattr(persona, name).items()]
    return out


@pytest.mark.parametrize(("name", "value"), _prompt_strings())
def test_prompts_have_no_answer_keys(name: str, value: str) -> None:
    for key in ANSWER_KEYS:
        assert key not in value.lower(), f"{name} mentions {key}"


def test_persona_models_have_no_answer_key_fields() -> None:
    models = [v for v in vars(persona).values() if isinstance(v, type) and issubclass(v, BaseModel)]
    assert models
    for model in models:
        for field in model.model_fields:
            assert "expected" not in field and "root_cause" not in field, f"{model.__name__}.{field}"


async def test_first_turn_and_follow_up_use_only_the_simulator_alias() -> None:
    llm = FakeLLMProvider(
        {"simulator": [structured(CustomerTurn(message="  my card keeps getting declined  ")), structured(CustomerTurn(done=True, satisfied=True))]}
    )  # fmt: skip
    customer = SimulatedCustomer(llm, view())
    first = await customer.next_turn([])
    assert first == CustomerTurn(message="my card keeps getting declined")
    lines = [ChatLine("customer", first.message), ChatLine("assistant", "Your card is frozen in the app.")]
    second = await customer.next_turn(lines)
    assert second.done and second.satisfied

    calls = llm.fake("simulator").calls
    assert persona.FIRST_USER in calls[0].text
    assert "You: my card keeps getting declined\nAssistant: Your card is frozen in the app." in calls[1].text
    for alias in ("triage", "assistant", "reasoning", "judge"):
        assert llm.fake(alias).calls == []


@pytest.mark.parametrize(
    ("scripted", "expected"),
    [
        (structured(CustomerTurn(message="")), CustomerTurn(done=True, satisfied=False)),  # nothing to say
        (structured(CustomerTurn(message="bye", done=True, satisfied=False)), CustomerTurn(done=True)),
        (text('{"message": "hello?", "done": false}'), CustomerTurn(message="hello?")),  # JSON in text
        (text("I am a language model."), CustomerTurn(done=True, satisfied=False)),  # unusable output
    ],
)
async def test_turn_parsing(scripted: object, expected: CustomerTurn) -> None:
    llm = FakeLLMProvider({"simulator": [scripted]})  # type: ignore[list-item]
    assert await SimulatedCustomer(llm, view()).next_turn([]) == expected
