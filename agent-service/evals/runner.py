"""The eval runner (architecture section 10, "Evaluation loop").

1. Load the scenarios (YAML, reviewed like code) and expand them into items, one per
   supported mode: `journey`, `assistant` (fixed script), `copilot` (fixed complaint).
2. With Langfuse: sync the items to a dataset (upserted on stable ids) and run them as
   one SDK experiment, so each item becomes a trace with its scores, grouped in a named
   run tagged with the profile, git commit and prompt versions. Without Langfuse: the
   same task and evaluators on the local backend.
3. Collect the scores into an `EvalReport`; the gate compares averages with thresholds.

Nothing that names the answer goes into what Langfuse shows as the item's or the
trace's input and metadata (the agents run inside the experiment trace): the dataset
item's `input` is an opaque item id and the mode, its metadata only the mode. The task
looks the public scenario up by that id. The scenario id, title and tags (which describe
the answer: `card-decline-fraud-rule`, `needs_human`) travel with the answer key in the
item's `expected_output`, which only the evaluator reads.
"""

import datetime as dt
import hashlib
import logging
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from evals.experiments import ExperimentBackend, LocalExperiments, item_field
from evals.redaction import redact_answer_key
from evals.report import EvalReport, ItemReport
from evals.results import Expected, ItemOutput, Mode
from evals.runtime import EvalRuntime, run_item
from evals.scenario import PublicScenario, Scenario
from evals.scorers import score_item

log = logging.getLogger(__name__)

DEFAULT_DATASET = "payments-support-scenarios"


class ItemInput(BaseModel):
    """A dataset item's input: an opaque id (the runner maps it to its scenario) and the mode."""

    item: str
    mode: Mode


class ItemExpected(Expected):
    """A dataset item's expected output: the answer key, and the scenario's labels."""

    scenario_id: str
    title: str = ""
    tags: list[str] = Field(default_factory=list)


def item_id(scenario_id: str, mode: Mode) -> str:
    """Stable (dataset upserts) but opaque: a scenario id often names its root cause."""
    digest = hashlib.sha256(f"{scenario_id}--{mode.value}".encode()).hexdigest()[:16]
    return f"item-{digest}"


@dataclass(frozen=True)
class EvalItem:
    id: str
    scenario: PublicScenario
    mode: Mode
    expected: Expected
    title: str = ""
    tags: tuple[str, ...] = ()

    @property
    def input(self) -> ItemInput:
        return ItemInput(item=self.id, mode=self.mode)

    def expected_output(self) -> dict[str, Any]:
        return ItemExpected(
            **self.expected.model_dump(), scenario_id=self.scenario.id, title=self.title, tags=list(self.tags)
        ).model_dump(mode="json")

    def metadata(self) -> dict[str, Any]:
        return {"mode": self.mode.value}

    def as_local(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input.model_dump(mode="json"),
            "expected_output": self.expected_output(),
            "metadata": self.metadata(),
        }


def build_items(scenarios: Sequence[Scenario], modes: Sequence[Mode]) -> list[EvalItem]:
    items: list[EvalItem] = []
    for scenario in scenarios:
        public = scenario.public()
        for mode in public.modes():
            if mode in modes:
                items.append(
                    EvalItem(
                        id=item_id(scenario.id, mode),
                        scenario=public,
                        mode=mode,
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
        self._items: dict[str, EvalItem] = {}  # this run's items by id, for the task

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
        known = self._items.get(data.item)
        if known is None:
            raise KeyError(f"eval item {data.item} is not part of this run")
        output = await run_item(self.runtime, known.scenario, data.mode)
        if output.error:
            log.warning("eval item %s/%s failed: %s", known.scenario.id, data.mode.value, output.error)
        # The output becomes the trace output: a second line of defence for the answer key.
        result: dict[str, Any] = redact_answer_key(output.model_dump(mode="json"))
        return result

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
                input=redact_answer_key(item.input.model_dump(mode="json")),
                expected_output=item.expected_output(),
                metadata=redact_answer_key(item.metadata()),
            )
        wanted = {item.id for item in items}
        dataset = client.get_dataset(name)
        return [i for i in dataset.items if getattr(i, "id", None) in wanted]

    # --- a run ------------------------------------------------------------------------------

    def run(self, config: RunConfig) -> EvalReport:
        items = build_items(config.scenarios, config.modes)
        self._items = {item.id: item for item in items}
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
        return self._report(run_name, metadata, result, items)

    def _report(self, run_name: str, metadata: dict[str, str], result: Any, items: Sequence[EvalItem]) -> EvalReport:
        """One report per item of the run, in order. An item missing from the results (the
        Langfuse SDK logs and drops items whose task raised) is reported as an error."""
        by_id: dict[str, ItemReport] = {}
        for item_result in getattr(result, "item_results", []):
            key = self._result_id(item_result.item)
            known = self._items.get(key or "")
            if known is None:
                log.warning("experiment returned an unknown item %s", key)
                continue
            raw = item_result.output
            output = ItemOutput.model_validate(raw) if raw is not None else None
            scores: dict[str, float] = {}
            comments: dict[str, str] = {}
            for evaluation in item_result.evaluations or []:
                if isinstance(evaluation.value, bool):
                    scores[evaluation.name] = 1.0 if evaluation.value else 0.0
                elif isinstance(evaluation.value, int | float):
                    scores[evaluation.name] = float(evaluation.value)
                if evaluation.comment:
                    comments[evaluation.name] = evaluation.comment
            by_id[known.id] = ItemReport(
                scenario_id=known.scenario.id,
                mode=known.mode,
                scores=scores,
                comments=comments,
                error=output.error if output else "the task produced no output",
                trace_id=getattr(item_result, "trace_id", None),
            )
        reports = [
            by_id.get(item.id)
            or ItemReport(scenario_id=item.scenario.id, mode=item.mode, error="no result: the task or the run failed")
            for item in items
        ]
        return EvalReport(run_name=run_name, metadata=metadata, items=reports)

    @staticmethod
    def _result_id(item: Any) -> str | None:
        data = item_field(item, "input")
        if isinstance(data, dict) and isinstance(data.get("item"), str):
            return str(data["item"])
        value = item_field(item, "id")
        return str(value) if value is not None else None
