"""The eval runner (architecture section 10, "Evaluation loop").

1. Load the scenarios (YAML, reviewed like code) and expand them into items, one per
   supported mode: `journey`, `assistant` (fixed script), `copilot` (fixed complaint).
2. With Langfuse: sync the items to a dataset (upserted on stable ids) and run them as
   one SDK experiment, so each item becomes a trace with its scores, grouped in a named
   run tagged with the profile, git commit and prompt versions. Without Langfuse: the
   same task and evaluators on the local backend.
3. Collect the scores into an `EvalReport`; the gate compares averages with thresholds.

The task receives only the dataset item's `input` (the scenario without `expected`);
the evaluator gets the `expected_output`.
"""

import datetime as dt
import logging
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from evals.experiments import ExperimentBackend, LocalExperiments, item_field
from evals.report import EvalReport, ItemReport
from evals.results import Expected, ItemOutput, Mode
from evals.runtime import EvalRuntime, run_item
from evals.scenario import PublicScenario, Scenario
from evals.scorers import score_item

log = logging.getLogger(__name__)

DEFAULT_DATASET = "payments-support-scenarios"


class ItemInput(BaseModel):
    """A dataset item's input: the scenario without its answer key, and the mode."""

    scenario: PublicScenario
    mode: Mode


@dataclass(frozen=True)
class EvalItem:
    id: str
    input: ItemInput
    expected: Expected
    title: str = ""
    tags: tuple[str, ...] = ()

    def as_local(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input.model_dump(mode="json"),
            "expected_output": self.expected.model_dump(mode="json"),
            "metadata": self.metadata(),
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "scenario_id": self.input.scenario.id,
            "mode": self.input.mode.value,
            "title": self.title,
            "tags": list(self.tags),
        }


def build_items(scenarios: Sequence[Scenario], modes: Sequence[Mode]) -> list[EvalItem]:
    items: list[EvalItem] = []
    for scenario in scenarios:
        public = scenario.public()
        for mode in public.modes():
            if mode in modes:
                items.append(
                    EvalItem(
                        id=f"{scenario.id}--{mode.value}",
                        input=ItemInput(scenario=public, mode=mode),
                        expected=scenario.expected,
                        title=scenario.title,
                        tags=tuple(scenario.tags),
                    )
                )
    return items


def git_commit() -> str:
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"][:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def prompt_versions() -> dict[str, str]:
    from assistant import prompts as assistant_prompts
    from core.prompts import guardrails, triage
    from evals import prompts as eval_prompts
    from investigation import prompts as investigation_prompts
    from simulation import persona

    return {
        "prompt_assistant": assistant_prompts.PROMPT_VERSION,
        "prompt_handoff_summary": assistant_prompts.SUMMARY_PROMPT_VERSION,
        "prompt_investigation": investigation_prompts.PROMPT_VERSION,
        "prompt_triage": triage.PROMPT_VERSION,
        "prompt_input_guard": guardrails.INPUT_GUARD_VERSION,
        "prompt_output_guard": guardrails.OUTPUT_GUARD_VERSION,
        "prompt_simulated_customer": persona.PROMPT_VERSION,
        "prompt_groundedness_judge": eval_prompts.GROUNDEDNESS_VERSION,
    }


@dataclass
class RunConfig:
    scenarios: list[Scenario]
    modes: list[Mode] = field(default_factory=lambda: list(Mode))
    run_name: str | None = None
    dataset: str = DEFAULT_DATASET
    max_concurrency: int = 1
    judge: bool = True


class EvalRunner:
    def __init__(self, runtime: EvalRuntime, *, langfuse: Any | None = None) -> None:
        """`langfuse`: a Langfuse client (datasets + experiments), or None for local runs.
        Defaults to the runtime's tracing client."""
        self.runtime = runtime
        self.langfuse = langfuse if langfuse is not None else runtime.tracing.client

    def metadata(self, run_name: str) -> dict[str, str]:
        return {
            "profile": self.runtime.settings.llm_profile,
            "git_commit": git_commit(),
            "run_name": run_name,
            **prompt_versions(),
        }

    # --- the experiment task and evaluator -----------------------------------------------

    async def task(self, *, item: Any, **_: Any) -> dict[str, Any]:
        data = ItemInput.model_validate(item_field(item, "input"))
        output = await run_item(self.runtime, data.scenario, data.mode)
        if output.error:
            log.warning("eval item %s/%s failed: %s", data.scenario.id, data.mode.value, output.error)
        return output.model_dump(mode="json")

    def evaluator(self, judge: bool) -> Any:
        from langfuse.experiment import Evaluation

        llm = self.runtime.llm if judge else None

        async def scores(*, output: Any, expected_output: Any, **_: Any) -> list[Evaluation]:
            result = ItemOutput.model_validate(output)
            expected = Expected.model_validate(expected_output)
            return [
                Evaluation(name=s.name, value=s.value, comment=s.comment)
                for s in await score_item(result, expected, llm)
            ]

        return scores

    # --- datasets ---------------------------------------------------------------------------

    def _sync_dataset(self, name: str, items: Sequence[EvalItem]) -> list[Any]:
        """Upsert the items into the Langfuse dataset; return its items for this run."""
        client = self.langfuse
        assert client is not None
        try:
            client.create_dataset(
                name=name,
                description="Payments support scenarios (agent-service/evals/scenarios), one item per mode.",
                metadata={"source": "agent-service/evals/scenarios"},
            )
        except Exception:
            log.info("dataset %s exists or could not be created; upserting items", name, exc_info=True)
        for item in items:
            client.create_dataset_item(
                dataset_name=name,
                id=item.id,
                input=item.input.model_dump(mode="json"),
                expected_output=item.expected.model_dump(mode="json"),
                metadata=item.metadata(),
            )
        wanted = {item.id for item in items}
        dataset = client.get_dataset(name)
        return [i for i in dataset.items if getattr(i, "id", None) in wanted]

    # --- a run ------------------------------------------------------------------------------

    def run(self, config: RunConfig) -> EvalReport:
        items = build_items(config.scenarios, config.modes)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        run_name = config.run_name or f"{self.runtime.settings.llm_profile}-{git_commit()}-{stamp}"
        metadata = self.metadata(run_name)
        backend: ExperimentBackend
        if self.langfuse is not None:
            backend = self.langfuse
            data: list[Any] = self._sync_dataset(config.dataset, items)
        else:
            backend = LocalExperiments()
            data = [item.as_local() for item in items]
        result = backend.run_experiment(
            name=config.dataset,
            run_name=run_name,
            description="Simulated journeys, assistant scripts and copilot complaints (architecture section 10).",
            data=data,
            task=self.task,
            evaluators=[self.evaluator(config.judge)],
            max_concurrency=config.max_concurrency,
            metadata=metadata,
        )
        if self.langfuse is not None:
            try:
                self.langfuse.flush()
            except Exception:
                log.warning("Langfuse flush failed", exc_info=True)
        return self._report(run_name, metadata, result)

    def _report(self, run_name: str, metadata: dict[str, str], result: Any) -> EvalReport:
        reports: list[ItemReport] = []
        for item_result in getattr(result, "item_results", []):
            raw = item_result.output
            output = ItemOutput.model_validate(raw) if raw is not None else None
            meta = item_field(item_result.item, "metadata") or {}
            scores: dict[str, float] = {}
            comments: dict[str, str] = {}
            for evaluation in item_result.evaluations or []:
                if isinstance(evaluation.value, int | float) and not isinstance(evaluation.value, bool):
                    scores[evaluation.name] = float(evaluation.value)
                elif isinstance(evaluation.value, bool):
                    scores[evaluation.name] = 1.0 if evaluation.value else 0.0
                if evaluation.comment:
                    comments[evaluation.name] = evaluation.comment
            reports.append(
                ItemReport(
                    scenario_id=output.scenario_id if output else str(meta.get("scenario_id")),
                    mode=output.mode if output else Mode(str(meta.get("mode", Mode.JOURNEY.value))),
                    scores=scores,
                    comments=comments,
                    error=output.error if output else "the task produced no output",
                    trace_id=getattr(item_result, "trace_id", None),
                )
            )
        return EvalReport(run_name=run_name, metadata=metadata, items=reports)
