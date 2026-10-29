"""Scorers, the report and the gate: deterministic scorers on hand-built outputs, the
groundedness judge on the fake LLM (`judge` alias)."""

from pathlib import Path

import pytest

from core.enums import ConversationOutcome, ExpectedOutcome, IssueType, RootCause
from evals import prompts as eval_prompts
from evals import scorers
from evals.report import EvalReport, ItemReport, check_gate, load_thresholds
from evals.results import AnswerOut, CaseOut, ConversationOut, Expected, ItemOutput, Mode
from evals.scorers import GroundednessVerdict, Score
from tests.fakes import FakeLLMProvider, structured, text

CARD_FROZEN = Expected(
    issue_type=IssueType.CARD_DECLINE, root_cause=RootCause.CARD_FROZEN_BY_CUSTOMER, outcome=ExpectedOutcome.SELF_SERVE
)
FRAUD = Expected(
    issue_type=IssueType.CARD_DECLINE, root_cause=RootCause.FRAUD_RULE_TRIGGERED, outcome=ExpectedOutcome.NEEDS_HUMAN
)


def conversation(
    outcome: ConversationOutcome | None, category: IssueType | None = IssueType.CARD_DECLINE
) -> ConversationOut:
    return ConversationOut(conversation_id="c-1", outcome=outcome, ended_by="test", category=category, turns=2)


def case(
    issue_type: IssueType | None = IssueType.CARD_DECLINE,
    root_cause: RootCause | None = RootCause.FRAUD_RULE_TRIGGERED,
    **kwargs: object,
) -> CaseOut:
    return CaseOut(
        case_id="k-1",
        case_status="awaiting_review",
        investigation_status="awaiting_review",
        issue_type=issue_type,
        diagnosis_issue_type=issue_type if root_cause else None,
        root_cause=root_cause,
        **kwargs,  # type: ignore[arg-type]
    )


def output(mode: Mode = Mode.JOURNEY, **kwargs: object) -> ItemOutput:
    return ItemOutput(mode=mode, **kwargs)  # type: ignore[arg-type]


# --- handoff_correct -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected", "value"),
    [
        (ConversationOutcome.HANDED_OFF, FRAUD, 1.0),
        (ConversationOutcome.RESOLVED, FRAUD, 0.0),
        (ConversationOutcome.ABANDONED, FRAUD, 0.0),
        (ConversationOutcome.RESOLVED, CARD_FROZEN, 1.0),
        (ConversationOutcome.ABANDONED, CARD_FROZEN, 1.0),  # no handoff: the right decision
        (ConversationOutcome.HANDED_OFF, CARD_FROZEN, 0.0),
    ],
)
def test_handoff_correct(outcome: ConversationOutcome, expected: Expected, value: float) -> None:
    score = scorers.handoff_correct(output(conversation=conversation(outcome)), expected)
    assert score is not None and score.name == "handoff_correct" and score.value == value


def test_handoff_correct_needs_a_conversation_and_skips_copilot_items() -> None:
    assert scorers.handoff_correct(output(), CARD_FROZEN) is None
    copilot = output(Mode.COPILOT, conversation=conversation(ConversationOutcome.HANDED_OFF))
    assert scorers.handoff_correct(copilot, CARD_FROZEN) is None


# --- containment ---------------------------------------------------------------------------


def test_containment_counts_resolved_self_serve_journeys_only() -> None:
    resolved = output(conversation=conversation(ConversationOutcome.RESOLVED))
    abandoned = output(conversation=conversation(ConversationOutcome.ABANDONED))
    assert scorers.containment(resolved, CARD_FROZEN) == Score("containment", 1.0, "resolved (test)")
    assert scorers.containment(abandoned, CARD_FROZEN).value == 0.0  # type: ignore[union-attr]
    assert scorers.containment(resolved, FRAUD) is None  # needs_human is not containable
    scripted = output(Mode.ASSISTANT, conversation=conversation(ConversationOutcome.ABANDONED))
    assert scorers.containment(scripted, CARD_FROZEN) is None  # scripts do not say if it helped


# --- categories ----------------------------------------------------------------------------


def test_assistant_category_correct() -> None:
    right = output(conversation=conversation(ConversationOutcome.RESOLVED, IssueType.CARD_DECLINE))
    wrong = output(conversation=conversation(ConversationOutcome.RESOLVED, IssueType.MISSED_PAYMENT))
    none = output(conversation=conversation(ConversationOutcome.RESOLVED, None))
    assert scorers.assistant_category_correct(right, CARD_FROZEN).value == 1.0  # type: ignore[union-attr]
    assert scorers.assistant_category_correct(wrong, CARD_FROZEN).value == 0.0  # type: ignore[union-attr]
    assert scorers.assistant_category_correct(none, CARD_FROZEN).value == 0.0  # type: ignore[union-attr]
    assert scorers.assistant_category_correct(output(Mode.COPILOT), CARD_FROZEN) is None


def test_copilot_category_correct() -> None:
    assert scorers.copilot_category_correct(output(case=case()), FRAUD).value == 1.0  # type: ignore[union-attr]
    other = output(case=case(IssueType.OTHER, None))
    assert scorers.copilot_category_correct(other, FRAUD).value == 0.0  # type: ignore[union-attr]
    assert scorers.copilot_category_correct(output(), FRAUD) is None  # no case (resolved journey)


# --- diagnosis_correct -----------------------------------------------------------------------


def test_diagnosis_correct_needs_issue_type_and_root_cause() -> None:
    assert scorers.diagnosis_correct(output(case=case()), FRAUD).value == 1.0  # type: ignore[union-attr]
    wrong_cause = output(case=case(root_cause=RootCause.CARD_FROZEN_BY_CUSTOMER))
    assert scorers.diagnosis_correct(wrong_cause, FRAUD).value == 0.0  # type: ignore[union-attr]
    wrong_type = output(case=case(IssueType.MISSED_PAYMENT, RootCause.FRAUD_RULE_TRIGGERED))
    assert scorers.diagnosis_correct(wrong_type, FRAUD).value == 0.0  # type: ignore[union-attr]


def test_diagnosis_correct_is_zero_for_escalations_and_parked_cases() -> None:
    escalated = scorers.diagnosis_correct(output(case=case(root_cause=None, escalated=True)), FRAUD)
    assert escalated == Score("diagnosis_correct", 0.0, "escalated")
    parked = scorers.diagnosis_correct(
        output(case=CaseOut(case_id="k", case_status="needs_manual", investigation_status="completed")), FRAUD
    )
    assert parked is not None and parked.value == 0.0 and "completed" in (parked.comment or "")
    assert scorers.diagnosis_correct(output(), FRAUD) is None


def test_building_blocks() -> None:
    assert scorers.handoff_value(True, ExpectedOutcome.NEEDS_HUMAN) == 1.0
    assert scorers.handoff_value(True, ExpectedOutcome.SELF_SERVE) == 0.0
    assert scorers.diagnosis_value(IssueType.CARD_DECLINE, RootCause.FRAUD_RULE_TRIGGERED, *_key(FRAUD)) == 1.0
    assert scorers.diagnosis_value(None, None, *_key(FRAUD)) == 0.0


def _key(expected: Expected) -> tuple[IssueType, RootCause]:
    return expected.issue_type, expected.root_cause


# --- tracked numbers ---------------------------------------------------------------------------


def test_tracked_scores() -> None:
    handed = output(case=case(provided_category=IssueType.CARD_DECLINE, category_changed=True, steps_used=3))
    assert scorers.category_changed(handed, FRAUD) == Score("category_changed", 1.0)
    assert scorers.steps_used(handed, FRAUD) == Score("steps_used", 3.0)
    email = output(Mode.COPILOT, case=case())
    assert scorers.category_changed(email, FRAUD) is None  # no assistant category to change
    assert scorers.item_ok(output(error="boom"), FRAUD) == Score("item_ok", 0.0, "boom")
    assert {"steps_used", "category_changed", "seconds"} <= scorers.TRACKED_ONLY


async def test_score_item_without_judge_runs_only_deterministic_scorers() -> None:
    item = output(conversation=conversation(ConversationOutcome.HANDED_OFF), case=case(), answers=[AnswerOut(text="x")])
    names = [s.name for s in await scorers.score_item(item, FRAUD, None)]
    assert names == ["item_ok", "handoff_correct", "assistant_category_correct", "copilot_category_correct",
                     "diagnosis_correct", "steps_used", "seconds"]  # fmt: skip


# --- groundedness (judge) ---------------------------------------------------------------------


async def test_groundedness_uses_the_judge_alias_and_averages_answers() -> None:
    llm = FakeLLMProvider(
        {
            "judge": [
                structured(GroundednessVerdict(claims=4, supported=3, unsupported_claims=["fee waived"])),
                structured(GroundednessVerdict(claims=0, supported=0)),
            ]
        }
    )
    item = output(
        answers=[
            AnswerOut(text="Your card 4111 1111 1111 1111 is frozen.", sources=[{"kind": "tool", "name": "cards", "content": {"status": "frozen"}}]),
            AnswerOut(text="Happy to help!", sources=[]),
        ]
    )  # fmt: skip
    score = await scorers.groundedness(llm, item)
    assert score is not None and score.name == "groundedness"
    assert score.value == pytest.approx((0.75 + 1.0) / 2)
    assert "fee waived" in (score.comment or "")
    calls = llm.fake("judge").calls
    assert len(calls) == 2
    assert calls[0].messages[0].content == eval_prompts.GROUNDEDNESS_SYSTEM
    assert "4111 1111 1111 1111" not in calls[0].text  # masked before the judge sees it
    assert "frozen" in calls[0].text
    for alias in ("triage", "assistant", "reasoning", "simulator"):
        assert llm.fake(alias).calls == []


async def test_groundedness_reads_json_answers_and_survives_judge_failures() -> None:
    llm = FakeLLMProvider({"judge": [text('{"claims": 2, "supported": 1}'), text("I cannot grade this.")]})
    item = output(answers=[AnswerOut(text="a"), AnswerOut(text="b")])
    score = await scorers.groundedness(llm, item)
    assert score is not None and score.value == 0.5 and "judge failed on 1" in (score.comment or "")

    all_fail = FakeLLMProvider({"judge": [text("no"), text("no")]})
    failed = await scorers.groundedness(all_fail, item)
    assert failed is not None and failed.value == 0.0
    assert await scorers.groundedness(llm, output()) is None  # no answers, nothing to judge


def test_groundedness_verdict_score_is_bounded() -> None:
    assert GroundednessVerdict(claims=2, supported=5).score == 1.0
    assert GroundednessVerdict(claims=0, supported=0).score == 1.0
    assert GroundednessVerdict(claims=4, supported=1).score == 0.25


# --- report and gate --------------------------------------------------------------------------


def _report(*items: tuple[Mode, dict[str, float]]) -> EvalReport:
    return EvalReport(
        run_name="r", items=[ItemReport(scenario_id=f"s{i}", mode=m, scores=s) for i, (m, s) in enumerate(items)]
    )


def test_report_summary_by_score_and_mode() -> None:
    report = _report(
        (Mode.JOURNEY, {"handoff_correct": 1.0, "steps_used": 4.0}),
        (Mode.ASSISTANT, {"handoff_correct": 0.0}),
        (Mode.JOURNEY, {"handoff_correct": 1.0}),
    )
    summary = report.summary()
    assert summary["handoff_correct"].mean == pytest.approx(2 / 3) and summary["handoff_correct"].count == 3
    assert report.summary(Mode.JOURNEY)["handoff_correct"].mean == 1.0
    assert "handoff_correct" in report.format() and "(tracked)" in report.format()


def test_gate_passes_fails_and_reports_unmeasured_scores() -> None:
    thresholds = {"handoff_correct": 0.8, "diagnosis_correct": 0.7}
    passing = check_gate(_report((Mode.JOURNEY, {"item_ok": 1.0, "handoff_correct": 1.0})), thresholds)
    assert passing.passed and passing.unchecked == ["diagnosis_correct"]

    failing = check_gate(
        _report(
            (Mode.JOURNEY, {"item_ok": 1.0, "handoff_correct": 1.0}),
            (Mode.JOURNEY, {"item_ok": 1.0, "handoff_correct": 0.0}),
        ),
        thresholds,
    )
    assert not failing.passed and failing.failures[0].startswith("handoff_correct: 0.500 < 0.800")
    assert "FAILED" in failing.format()

    assert not check_gate(EvalReport(run_name="empty"), thresholds).passed


def test_gate_fails_when_an_item_was_not_scored() -> None:
    """An item with no scores (its evaluator raised) used to pass every threshold."""
    gate = check_gate(_report((Mode.JOURNEY, {})), load_thresholds())
    assert not gate.passed
    assert any("no item_ok score" in f and "s0/journey" in f for f in gate.failures)

    one_missing = check_gate(
        _report((Mode.JOURNEY, {"item_ok": 1.0, "handoff_correct": 1.0}), (Mode.ASSISTANT, {})),
        {"handoff_correct": 0.8},
    )
    assert not one_missing.passed and any("1 item(s) have no item_ok" in f for f in one_missing.failures)


def test_gate_fails_when_no_threshold_was_measured() -> None:
    gate = check_gate(_report((Mode.COPILOT, {"item_ok": 1.0})), {"handoff_correct": 0.8, "diagnosis_correct": 0.7})
    assert not gate.passed and gate.failures == ["no threshold was measured in this run"]
    assert gate.unchecked == ["diagnosis_correct", "handoff_correct"]
    assert not check_gate(_report((Mode.COPILOT, {"item_ok": 1.0})), {}).passed


def test_gate_fails_on_thresholds_that_match_no_scorer() -> None:
    report = _report((Mode.JOURNEY, {"item_ok": 1.0, "handoff_correct": 1.0}))
    typo = check_gate(report, {"handoff_correct": 0.8, "handof_correct": 0.8})
    assert not typo.passed and typo.failures == ["threshold 'handof_correct' matches no scorer"]
    tracked = check_gate(report, {"handoff_correct": 0.8, "seconds": 10.0})
    assert not tracked.passed and tracked.failures == ["threshold 'seconds' is tracked, not gated"]


def test_score_names_match_their_scorers() -> None:
    assert {
        "item_ok",
        "handoff_correct",
        "containment",
        "assistant_category_correct",
        "copilot_category_correct",
        "diagnosis_correct",
        "groundedness",
    } == scorers.GATED_SCORES
    assert set(load_thresholds()) <= scorers.GATED_SCORES


def test_thresholds_file_gates_only_real_scores(tmp_path: Path) -> None:
    thresholds = load_thresholds()
    assert {"handoff_correct", "diagnosis_correct", "groundedness", "item_ok"} <= set(thresholds)
    assert not set(thresholds) & scorers.TRACKED_ONLY
    assert all(0.0 <= v <= 1.0 for v in thresholds.values())
    custom = tmp_path / "t.yaml"
    custom.write_text("thresholds:\n  handoff_correct: 0.5\n", encoding="utf-8")
    assert load_thresholds(custom) == {"handoff_correct": 0.5}
