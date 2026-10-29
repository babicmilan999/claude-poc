"""Scenario files: valid against `contracts/scenario.schema.json`, one per row and root
cause of the section 5 table, and loaded without their answer key for the task."""

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from core.enums import ExpectedOutcome, IssueType, RootCause
from evals.results import Mode
from evals.scenario import SCENARIOS_DIR, ScenarioError, load_scenario, load_scenarios
from tests.banking_mock import CONTRACTS_DIR, load_json

FILES = sorted(SCENARIOS_DIR.glob("*.yaml"))

# Architecture section 5: (scenario type, root cause, expected outcome).
SECTION_5_ROWS = {
    (IssueType.CARD_DECLINE, RootCause.CARD_FROZEN_BY_CUSTOMER, ExpectedOutcome.SELF_SERVE),
    (IssueType.CARD_DECLINE, RootCause.INSUFFICIENT_FUNDS, ExpectedOutcome.SELF_SERVE),
    (IssueType.CARD_DECLINE, RootCause.CARD_EXPIRED, ExpectedOutcome.SELF_SERVE),
    (IssueType.CARD_DECLINE, RootCause.CONTACTLESS_LIMIT_EXCEEDED, ExpectedOutcome.SELF_SERVE),
    (IssueType.CARD_DECLINE, RootCause.MERCHANT_CATEGORY_BLOCKED, ExpectedOutcome.SELF_SERVE),
    (IssueType.CARD_DECLINE, RootCause.FRAUD_RULE_TRIGGERED, ExpectedOutcome.NEEDS_HUMAN),
    (IssueType.MISSED_PAYMENT, RootCause.INSUFFICIENT_FUNDS, ExpectedOutcome.SELF_SERVE),
    (IssueType.MISSED_PAYMENT, RootCause.DAILY_PAYMENT_LIMIT_EXCEEDED, ExpectedOutcome.SELF_SERVE),
    (IssueType.MISSED_PAYMENT, RootCause.PAYEE_ACCOUNT_CLOSED, ExpectedOutcome.NEEDS_HUMAN),
    (IssueType.FAILED_DIRECT_DEBIT, RootCause.MANDATE_CANCELLED_BY_CUSTOMER, ExpectedOutcome.SELF_SERVE),
    (IssueType.FAILED_DIRECT_DEBIT, RootCause.INSUFFICIENT_FUNDS, ExpectedOutcome.SELF_SERVE),
    (IssueType.FAILED_DIRECT_DEBIT, RootCause.ACCOUNT_CLOSED_BY_ORIGINATOR, ExpectedOutcome.NEEDS_HUMAN),
}


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    return Draft202012Validator(load_json("scenario.schema.json"))


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_scenario_file_matches_the_contract(path: Path, validator: Draft202012Validator) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(data)]
    assert not errors, errors
    scenario = load_scenario(path)
    assert scenario.id == path.stem
    # The incident the banking service sets up is the one the answer key describes.
    assert (scenario.incident.type, scenario.incident.root_cause) == (
        scenario.expected.issue_type,
        scenario.expected.root_cause,
    )
    assert scenario.expected.outcome.value in scenario.tags
    # Every scenario runs as a journey and a copilot case; with a script also as an assistant eval.
    modes = scenario.public().modes()
    assert Mode.JOURNEY in modes and Mode.COPILOT in modes
    assert (Mode.ASSISTANT in modes) == bool(scenario.conversation_script)


def test_every_section_5_row_has_a_scenario() -> None:
    covered = {(s.expected.issue_type, s.expected.root_cause, s.expected.outcome) for s in load_scenarios()}
    assert covered == SECTION_5_ROWS


def test_contract_examples_are_part_of_the_eval_set() -> None:
    for example in sorted((CONTRACTS_DIR / "examples" / "scenarios").glob("*.yaml")):
        ours = SCENARIOS_DIR / example.name
        assert ours.exists(), example.name
        assert yaml.safe_load(ours.read_text()) == yaml.safe_load(example.read_text())


# Words that would hand the agents the root cause if the customer said them before the
# assistant explained anything (complaint, phrasing, first scripted message).
GIVEAWAYS: dict[RootCause, tuple[str, ...]] = {
    RootCause.CARD_FROZEN_BY_CUSTOMER: ("froze", "frozen", "freez"),
    RootCause.INSUFFICIENT_FUNDS: ("insufficient", "not enough money", "low balance", "overdrawn", "no money"),
    RootCause.CARD_EXPIRED: ("expired", "expiry", "out of date", "new card"),
    RootCause.CONTACTLESS_LIMIT_EXCEEDED: ("limit",),
    RootCause.MERCHANT_CATEGORY_BLOCKED: ("blocked", "gambling", "category", "spending control"),
    RootCause.FRAUD_RULE_TRIGGERED: ("fraud", "suspicious", "security check"),
    RootCause.DAILY_PAYMENT_LIMIT_EXCEEDED: ("limit", "too many payments", "savings"),
    RootCause.PAYEE_ACCOUNT_CLOSED: ("closed", "shut"),
    RootCause.MANDATE_CANCELLED_BY_CUSTOMER: ("cancelled", "mandate"),
    RootCause.ACCOUNT_CLOSED_BY_ORIGINATOR: ("closed", "originator"),
}
# The customer guessing at a cause, in any scripted message.
SPECULATION = ("is that the problem", "is that why", "is it because", "was it because", "is that the reason")


def test_giveaways_cover_every_root_cause() -> None:
    assert set(GIVEAWAYS) == {s.expected.root_cause for s in load_scenarios()}


def test_customer_knowledge_never_states_the_root_cause() -> None:
    for scenario in load_scenarios():
        script = scenario.conversation_script or []
        words = " ".join([*scenario.persona.knows, *scenario.persona.phrasing, *script])
        words += " " + (scenario.complaint or "")
        assert scenario.expected.root_cause.value not in words.lower().replace(" ", "_"), scenario.id

        opening = " ".join([*scenario.persona.phrasing, *script[:1], scenario.complaint or ""]).lower()
        for phrase in GIVEAWAYS[scenario.expected.root_cause]:
            assert phrase not in opening, f"{scenario.id}: the customer opens with {phrase!r}"
        for line in script:
            for phrase in SPECULATION:
                assert phrase not in line.lower(), f"{scenario.id}: the script guesses the cause: {line!r}"


def test_public_scenario_has_no_answer_key() -> None:
    scenario = load_scenarios(only=["card-decline-fraud-rule"])[0]
    public = scenario.public().model_dump(mode="json")
    assert set(public) == {"id", "incident", "complaint", "persona", "conversation_script"}
    assert "needs_human" not in str(public) and "self_serve" not in str(public)


def test_loader_rejects_bad_files(tmp_path: Path) -> None:
    good = yaml.safe_load((SCENARIOS_DIR / "card-decline-card-frozen.yaml").read_text())
    renamed = tmp_path / "another-name.yaml"
    renamed.write_text(yaml.safe_dump(good))
    with pytest.raises(ScenarioError, match="must match the file name"):
        load_scenario(renamed)
    bad = tmp_path / "card-decline-card-frozen.yaml"
    bad.write_text(yaml.safe_dump({**good, "surprise": True}))
    with pytest.raises(ScenarioError):
        load_scenario(bad)
    with pytest.raises(ScenarioError, match="unknown scenario"):
        load_scenarios(only=["no-such-scenario"])
