"""Eval and simulation scenarios (`contracts/scenario.schema.json`), loaded from YAML
files in `evals/scenarios/`.

`Scenario.public()` is what the experiment task gets: the incident to set up, the persona,
the script and the complaint. The `expected` block travels separately, as the dataset
item's expected output, and only scorers read it. Title, description and tags often
describe the answer ("declined because the customer froze it", `needs_human`), so they go
to the item's metadata, not to the task.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from core.enums import IssueType, RootCause
from evals.results import Expected, Mode
from simulation.persona import PersonaSpec

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


class IncidentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: IssueType
    root_cause: RootCause
    params: dict[str, Any] | None = None


class PublicScenario(BaseModel):
    """What it takes to run a scenario, without its answer key or labels."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    incident: IncidentSpec
    complaint: str | None = None
    persona: PersonaSpec
    conversation_script: list[str] | None = Field(default=None, min_length=1)

    def modes(self) -> list[Mode]:
        """The eval modes this scenario supports."""
        out = [Mode.JOURNEY]
        if self.conversation_script:
            out.append(Mode.ASSISTANT)
        if self.complaint:
            out.append(Mode.COPILOT)
        return out


class Scenario(PublicScenario):
    title: str = Field(min_length=1)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    expected: Expected

    def public(self) -> PublicScenario:
        return PublicScenario.model_validate(self.model_dump(exclude={"title", "description", "tags", "expected"}))


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        scenario = Scenario.model_validate(data)
    except ValueError as exc:
        raise ScenarioError(f"{path.name}: {exc}") from exc
    if scenario.id != path.stem:
        raise ScenarioError(f"{path.name}: id {scenario.id!r} must match the file name")
    return scenario


def load_scenarios(directory: Path = SCENARIOS_DIR, only: list[str] | None = None) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
    if only:
        wanted = set(only)
        unknown = wanted - {s.id for s in scenarios}
        if unknown:
            raise ScenarioError(f"unknown scenario id(s): {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s.id in wanted]
    return scenarios
