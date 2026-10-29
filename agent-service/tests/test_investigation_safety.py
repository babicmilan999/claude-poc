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


# --- streamed tokens are masked across chunk boundaries ----------------------------------

CARD = "4111 1111 1111 1111"  # Luhn-valid


async def test_a_card_number_split_across_streamed_chunks_is_masked(banking: object) -> None:
    """The fake LLM streams one chunk per word, so the card number arrives as four
    chunks; neither the stored `token` events nor the explanation may contain it."""
    from core.enums import IssueType, RootCause
    from tests.banking_mock import ALICE
    from tests.fakes import FakeLLMProvider, text, tool_call
    from tests.investigation.harness import diagnosis, enough, make_harness, triaged

    llm = FakeLLMProvider()
    h = make_harness(llm, banking)  # type: ignore[arg-type]
    case_id = h.store.add_case(customer_id=ALICE, complaint="card declined")
    llm.script("triage", triaged(IssueType.CARD_DECLINE))
    llm.script(
        "reasoning",
        tool_call("get_cards", {}),
        text("Card frozen (ev-1)."),
        enough("ev-1"),
        diagnosis(IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, "ev-1"),
        text(f"The card {CARD} was frozen by the customer (ev-1), account 12345678 too."),
    )
    assert await h.service.run_pending() == 1
    inv = h.only_investigation(case_id)
    tokens = [e.payload["text"] for e in h.events(inv.id) if e.type == "token"]
    assert len(tokens) > 1, "still streamed in pieces"
    streamed = "".join(tokens)
    for piece in tokens:
        assert "1111" not in piece.replace("****1111", ""), piece
    assert "4111" not in streamed and "12345678" not in streamed
    assert "[CARD ****1111]" in streamed and "****5678" in streamed
    assert streamed.strip() == inv.diagnosis["explanation"]


@pytest.mark.parametrize("seed", range(25))
def test_masked_token_stream_matches_masking_the_whole_text(seed: int) -> None:
    import random

    from core.masking import mask_text
    from investigation.common import MaskedTokenStream

    text = (
        "Card 4111111111111111 and 4111 1111 1111 1111 were declined; account number 12345678, "
        "sort code 12-34-56, IBAN GB29 NWBK 6016 1331 9268 19, cvv is 123, phone 07700 900 123, "
        "mail a.b@example.com. Done."
    )
    rng = random.Random(seed)
    stream, out, i = MaskedTokenStream(), [], 0
    while i < len(text):
        n = rng.randint(1, 7)
        out.append(stream.push(text[i : i + n]))
        i += n
    out.append(stream.flush())
    assert "".join(out) == mask_text(text)
    for piece in out:
        assert mask_text(piece) == piece, "every emitted piece is already masked"
