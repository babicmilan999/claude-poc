"""The eval runner end to end on the fake LLM and the banking API mocked from the contract
(with the simulation endpoint): simulated journeys (customer → assistant → handoff →
copilot), assistant scripts and copilot complaints, scored and gated; Langfuse datasets
and experiments through a fake client; and no answer key in anything a model was sent."""

import json
from typing import Any

import pytest

from evals.__main__ import main as cli_main
from evals.report import check_gate, load_thresholds
from evals.results import Mode
from evals.runner import EvalRunner, RunConfig, build_items, item_id
from evals.scenario import load_scenarios
from evals.scorers import SCORE_NAMES
from tests.evals.fake_models import calls_by_alias, perfect_models
from tests.evals.helpers import FakeLangfuse, make_runtime
from tests.fakes import FakeLLMProvider
from tests.simulation.banking_sim_mock import SimulationBankingMock

FROZEN, FRAUD, MANDATE = "card-decline-card-frozen", "card-decline-fraud-rule", "failed-dd-mandate-cancelled"


def _run(llm: FakeLLMProvider, mock: SimulationBankingMock, only: list[str], **config: Any) -> Any:
    runner = EvalRunner(make_runtime(llm, mock), langfuse=config.pop("langfuse", None))
    return runner.run(RunConfig(scenarios=load_scenarios(only=only), run_name="test-run", **config))


@pytest.fixture(scope="module")
def frozen_and_fraud() -> tuple[Any, FakeLLMProvider, SimulationBankingMock]:
    llm, mock = perfect_models(), SimulationBankingMock()
    return _run(llm, mock, [FROZEN, FRAUD]), llm, mock


def _item(report: Any, scenario: str, mode: Mode) -> Any:
    return next(i for i in report.items if i.scenario_id == scenario and i.mode is mode)


def test_runner_end_to_end_scores_every_mode_and_passes_the_gate(frozen_and_fraud: Any) -> None:
    report, _, mock = frozen_and_fraud
    assert {(i.scenario_id, i.mode) for i in report.items} == {(s, m) for s in (FROZEN, FRAUD) for m in Mode}
    assert all(i.error is None for i in report.items), [i.error for i in report.items]
    assert all(set(i.scores) <= SCORE_NAMES for i in report.items)  # what the gate knows

    # Self-serve journey: the simulated customer was answered and left satisfied, no case.
    frozen = _item(report, FROZEN, Mode.JOURNEY)
    assert frozen.scores["handoff_correct"] == 1.0 and frozen.scores["containment"] == 1.0
    assert frozen.scores["groundedness"] == 1.0
    assert "diagnosis_correct" not in frozen.scores

    # Needs-human journey: handed off, the case investigated, the diagnosis matches.
    fraud = _item(report, FRAUD, Mode.JOURNEY)
    assert fraud.scores["handoff_correct"] == 1.0
    assert fraud.scores["diagnosis_correct"] == 1.0 and fraud.scores["copilot_category_correct"] == 1.0
    assert fraud.scores["category_changed"] == 0.0 and fraud.scores["steps_used"] >= 1
    assert "containment" not in fraud.scores

    # Fixed script: the fraud block is handed off on the first message.
    assert _item(report, FRAUD, Mode.ASSISTANT).scores["handoff_correct"] == 1.0
    assert _item(report, FROZEN, Mode.ASSISTANT).scores["handoff_correct"] == 1.0
    # Fixed complaint: copilot only.
    copilot = _item(report, FROZEN, Mode.COPILOT)
    assert copilot.scores["diagnosis_correct"] == 1.0 and "handoff_correct" not in copilot.scores

    gate = check_gate(report, load_thresholds())
    assert gate.passed, gate.format()
    assert gate.unchecked == []

    # Each item set up its own incident through the contract's simulation endpoint.
    assert len(mock.incident_requests) == len(report.items)
    assert {"type", "root_cause"} <= set(mock.incident_requests[0])
    assert report.metadata["profile"] == "test" and report.metadata["run_name"] == "test-run"
    assert report.metadata["prompt_simulated_customer"].startswith("simulated-customer")


def test_simulated_customer_chats_through_the_assistant(frozen_and_fraud: Any) -> None:
    _, llm, _ = frozen_and_fraud
    calls = calls_by_alias(llm)
    simulator_calls = [c.text for c in calls["simulator"]]
    assert any("the card ending 4242 was declined" in t for t in simulator_calls)  # the incident's facts
    assert any("Assistant:" in t for t in simulator_calls)  # it read the assistant's answer
    # The simulated customer's first message reached the assistant's guard and triage.
    assert any("Hello, " in c.text for c in calls["triage"])


def test_no_answer_key_reaches_any_model(frozen_and_fraud: Any) -> None:
    """Scan everything every model was sent (messages and tool schemas)."""
    _report, llm, mock = frozen_and_fraud
    calls = calls_by_alias(llm)
    assert all(calls.values()), {a: len(c) for a, c in calls.items()}  # every alias was used
    expected_root_causes = {i["expected_root_cause"] for i in mock.incidents}
    for alias, alias_calls in calls.items():
        for call in alias_calls:
            surface = call.text + json.dumps(call.kwargs, default=str)
            for key in ("expected_root_cause", "expected_outcome", "expected_output", "answer key"):
                assert key not in surface, f"{alias} was sent {key!r}"
            for outcome in ("self_serve", "needs_human"):
                assert outcome not in call.text, f"{alias} was sent the expected outcome {outcome!r}"
            if alias == "reasoning":
                continue  # its system prompts list every root-cause code; checked below
            for root in expected_root_causes:
                assert root not in call.text, f"{alias} was sent the root cause {root!r}"
    # The copilot is told the codes it may choose from, but no case text names the
    # expected one before its own diagnosis is written.
    for call in calls["reasoning"]:
        system = str(call.messages[0].content)
        if system.startswith("You explain a diagnosis"):
            continue  # gets its own diagnosis back to explain
        rest = "\n".join(str(m.content) for m in call.messages[1:])
        for root in expected_root_causes:
            assert root not in rest, f"reasoning was sent the root cause {root!r} outside its prompt"


def test_gate_fails_when_a_score_drops() -> None:
    """The fake triage reads "my rent didn't go out" as a missed payment; the facts say a
    failed Direct Debit, so the journey's category score drops and the gate fails."""
    report = _run(perfect_models(), SimulationBankingMock(), [MANDATE], modes=[Mode.JOURNEY])
    item = _item(report, MANDATE, Mode.JOURNEY)
    assert item.scores["assistant_category_correct"] == 0.0
    assert item.scores["handoff_correct"] == 1.0
    gate = check_gate(report, load_thresholds())
    assert not gate.passed
    assert any(f.startswith("assistant_category_correct") for f in gate.failures)
    assert "diagnosis_correct" in gate.unchecked  # no case: the journey was self-serve


def test_item_errors_are_reported_not_raised() -> None:
    mock = SimulationBankingMock()
    mock.simulation_enabled = False  # the banking service runs without its simulation profile
    report = _run(perfect_models(), mock, [FROZEN], modes=[Mode.COPILOT], judge=False)
    item = _item(report, FROZEN, Mode.COPILOT)
    assert item.scores["item_ok"] == 0.0 and "SimulationUnavailable" in (item.error or "")
    assert not check_gate(report, load_thresholds()).passed


def test_runner_with_langfuse_syncs_the_dataset_and_runs_one_experiment() -> None:
    langfuse = FakeLangfuse()
    report = _run(
        perfect_models(), SimulationBankingMock(), [FROZEN], modes=[Mode.ASSISTANT, Mode.COPILOT], langfuse=langfuse
    )

    assert set(langfuse.datasets) == {"payments-support-scenarios"}
    copilot_id = item_id(FROZEN, Mode.COPILOT)
    assert set(langfuse.items) == {item_id(FROZEN, Mode.ASSISTANT), copilot_id}
    item = langfuse.items[copilot_id]
    # The answer key and the labels that describe it travel only as the expected output.
    assert item["expected_output"] == {
        "issue_type": "card_decline",
        "root_cause": "card_frozen_by_customer",
        "outcome": "self_serve",
        "scenario_id": FROZEN,
        "title": "Card declined because the customer froze it",
        "tags": ["card_decline", "self_serve"],
    }
    assert item["input"] == {"item": copilot_id, "mode": "copilot"}
    assert item["metadata"] == {"mode": "copilot"}
    assert {(i.scenario_id, i.mode) for i in report.items} == {(FROZEN, Mode.ASSISTANT), (FROZEN, Mode.COPILOT)}

    (experiment,) = langfuse.experiments
    assert experiment["name"] == "payments-support-scenarios" and experiment["run_name"] == "test-run"
    assert {"profile", "git_commit", "prompt_assistant", "prompt_investigation"} <= set(experiment["metadata"])
    assert all(isinstance(v, str) for v in experiment["metadata"].values())
    assert langfuse.flushed == 1
    assert all(i.trace_id for i in report.items)
    assert check_gate(report, load_thresholds()).passed


def test_build_items_expands_modes() -> None:
    scenarios = load_scenarios(only=[FROZEN, MANDATE])
    items = build_items(scenarios, [Mode.ASSISTANT, Mode.COPILOT])
    # The mandate example has no conversation script: no assistant item for it.
    assert [(i.scenario.id, i.mode) for i in items] == [
        (FROZEN, Mode.ASSISTANT),
        (FROZEN, Mode.COPILOT),
        (MANDATE, Mode.COPILOT),
    ]
    # Ids are stable (dataset upserts) and opaque (a scenario id names its root cause).
    assert [i.id for i in items] == [i.id for i in build_items(scenarios, [Mode.ASSISTANT, Mode.COPILOT])]
    assert len({i.id for i in items}) == 3
    assert all(i.id.startswith("item-") and "card" not in i.id and "mandate" not in i.id for i in items)


def test_cli_rejects_unknown_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--only", "no-such-scenario"]) == 2
    assert "unknown scenario" in capsys.readouterr().err
    assert cli_main(["--modes", "everything"]) == 2


# --- the gate cannot pass on missing scores --------------------------------------------------


def test_gate_fails_when_the_evaluator_raises_under_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Langfuse's `run_experiment` logs evaluator errors and drops their scores; the fake
    does the same. A broken evaluator must not leave a green gate."""

    def broken(self: EvalRunner, judge: bool) -> Any:
        async def scores(**_: Any) -> list[Any]:
            raise RuntimeError("evaluator bug")

        return scores

    monkeypatch.setattr(EvalRunner, "evaluator", broken)
    langfuse = FakeLangfuse()
    report = _run(perfect_models(), SimulationBankingMock(), [FROZEN], modes=[Mode.COPILOT], langfuse=langfuse)
    (item,) = report.items
    assert item.scores == {} and item.error is None  # the task ran; nothing was scored
    gate = check_gate(report, load_thresholds())
    assert not gate.passed
    assert any("no item_ok score" in f and f"{FROZEN}/copilot" in f for f in gate.failures)
    assert any("no threshold was measured" in f for f in gate.failures)


def test_item_whose_task_raises_is_reported_and_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK drops an item whose task raised; the report still lists it, as an error."""

    async def broken(self: EvalRunner, **_: Any) -> Any:
        raise RuntimeError("task bug")

    monkeypatch.setattr(EvalRunner, "task", broken)
    report = _run(perfect_models(), SimulationBankingMock(), [FROZEN], modes=[Mode.COPILOT], langfuse=FakeLangfuse())
    (item,) = report.items
    assert item.scenario_id == FROZEN and "no result" in (item.error or "")
    assert not check_gate(report, load_thresholds()).passed


# --- no answer in anything Langfuse records -----------------------------------------------


def _answers() -> list[str]:
    """Every scenario's root cause, expected outcome and the ids / titles naming them."""
    out: set[str] = set()
    for s in load_scenarios():
        out |= {s.expected.root_cause.value, s.incident.root_cause.value, s.expected.outcome.value, s.id, s.title}
    return sorted(out)


def test_no_root_cause_or_outcome_in_langfuse_inputs_or_metadata() -> None:
    """Dataset item inputs and metadata, and every trace's input, metadata and task
    observation input: no scenario's root cause, expected outcome, id or title. (The
    evaluator observation gets `expected_output` by the SDK's design: it is the scorer.)"""
    langfuse = FakeLangfuse()
    report = _run(perfect_models(), SimulationBankingMock(), [FROZEN, FRAUD], langfuse=langfuse)
    assert len(report.items) == len(langfuse.traces) == 6
    assert check_gate(report, load_thresholds()).passed

    surfaces: list[tuple[str, Any]] = []
    for key, item in langfuse.items.items():
        surfaces += [(f"item {key} input", item["input"]), (f"item {key} metadata", item["metadata"])]
    for n, trace in enumerate(langfuse.traces):
        surfaces += [(f"trace {n} input", trace["input"]), (f"trace {n} metadata", trace["metadata"])]
        surfaces += [
            (f"trace {n} {o['name']} input", o["input"])
            for o in trace["observations"]
            if o.get("as_type") != "evaluator"
        ]
        surfaces.append((f"trace {n} output", {k: v for k, v in trace["output"].items() if k != "case"}))
    for where, value in surfaces:
        dumped = json.dumps(value, default=str)
        for answer in _answers():
            assert answer not in dumped, f"{where} contains {answer!r}"


def test_eval_mask_redacts_answers_inside_incident_and_expected_blocks() -> None:
    from core.masking import REDACTED
    from evals.redaction import eval_langfuse_mask

    scenario = load_scenarios(only=[FRAUD])[0]
    data = {
        "scenario": scenario.model_dump(mode="json"),
        "expected_output": {"root_cause": "fraud_rule_triggered", "outcome": "needs_human"},
        "case": {"root_cause": "fraud_rule_triggered"},  # the copilot's own diagnosis stays
        "note": "card 4242 4242 4242 4242",
    }
    masked = eval_langfuse_mask(data=data)
    assert masked["scenario"]["incident"]["root_cause"] == REDACTED
    assert masked["scenario"]["expected"] == {"issue_type": "card_decline", "root_cause": REDACTED, "outcome": REDACTED}
    assert masked["expected_output"]["root_cause"] == REDACTED and masked["expected_output"]["outcome"] == REDACTED
    assert masked["case"]["root_cause"] == "fraud_rule_triggered"
    assert "4242 4242" not in masked["note"]  # the core mask still runs
