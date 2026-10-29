"""Eval results: per item, averaged per score, and the gate (thresholds from
`evals/thresholds.yaml`)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from evals.results import Mode
from evals.scorers import TRACKED_ONLY

THRESHOLDS_FILE = Path(__file__).resolve().parent / "thresholds.yaml"


class ItemReport(BaseModel):
    scenario_id: str
    mode: Mode
    scores: dict[str, float] = Field(default_factory=dict)
    comments: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    trace_id: str | None = None


class ScoreSummary(BaseModel):
    name: str
    mean: float
    count: int


class EvalReport(BaseModel):
    run_name: str
    metadata: dict[str, str] = Field(default_factory=dict)
    items: list[ItemReport] = Field(default_factory=list)

    def summary(self, mode: Mode | None = None) -> dict[str, ScoreSummary]:
        values: dict[str, list[float]] = {}
        for item in self.items:
            if mode is not None and item.mode is not mode:
                continue
            for name, value in item.scores.items():
                values.setdefault(name, []).append(value)
        return {name: ScoreSummary(name=name, mean=sum(v) / len(v), count=len(v)) for name, v in sorted(values.items())}

    def format(self) -> str:
        lines = [f"Eval run {self.run_name}"]
        lines += [f"  {k}: {v}" for k, v in self.metadata.items() if not k.startswith("prompt_")]
        lines.append("")
        lines.append(f"{'score':<28}{'mean':>8}{'n':>5}   by mode")
        for name, total in self.summary().items():
            by_mode = []
            for mode in Mode:
                part = self.summary(mode).get(name)
                if part is not None:
                    by_mode.append(f"{mode.value} {part.mean:.2f} ({part.count})")
            tracked = " (tracked)" if name in TRACKED_ONLY else ""
            lines.append(f"{name:<28}{total.mean:>8.2f}{total.count:>5}   {', '.join(by_mode)}{tracked}")
        failed = [i for i in self.items if i.error]
        if failed:
            lines.append("")
            lines.append("Errors:")
            lines += [f"  {i.scenario_id}/{i.mode.value}: {i.error}" for i in failed]
        misses = [
            (i, name)
            for i in self.items
            for name, value in i.scores.items()
            if name not in TRACKED_ONLY and value < 1.0 and name != "item_ok"
        ]
        if misses:
            lines.append("")
            lines.append("Below 1.0:")
            lines += [
                f"  {i.scenario_id}/{i.mode.value} {name}={i.scores[name]:.2f}: {i.comments.get(name, '')}"
                for i, name in misses
            ]
        return "\n".join(lines)


class GateResult(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)
    unchecked: list[str] = Field(default_factory=list)  # thresholds with no scored items

    def format(self) -> str:
        text = "Gate passed." if self.passed else "Gate FAILED:\n" + "\n".join(f"  {f}" for f in self.failures)
        if self.unchecked:
            text += "\nNot measured in this run: " + ", ".join(self.unchecked)
        return text


def load_thresholds(path: Path = THRESHOLDS_FILE) -> dict[str, float]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    thresholds = data.get("thresholds") or {}
    return {str(k): float(v) for k, v in thresholds.items()}


def check_gate(report: EvalReport, thresholds: dict[str, float]) -> GateResult:
    """Every threshold's average must be met. A score no item produced is reported as not
    measured (for example `diagnosis_correct` in an assistant-only run), not as a failure;
    a run with no items at all fails."""
    if not report.items:
        return GateResult(passed=False, failures=["no eval items ran"])
    summary = report.summary()
    failures: list[str] = []
    unchecked: list[str] = []
    for name, minimum in sorted(thresholds.items()):
        score = summary.get(name)
        if score is None:
            unchecked.append(name)
        elif score.mean < minimum:
            failures.append(f"{name}: {score.mean:.3f} < {minimum:.3f} ({score.count} item(s))")
    return GateResult(passed=not failures, failures=failures, unchecked=unchecked)
