"""Eval runtime wired with fakes (fake LLM, contract banking mock with the simulation
endpoint, fixture knowledge base), and a fake Langfuse client: no network."""

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from core.kb import KnowledgeBaseSearch
from core.kb.embeddings import HashingEmbedder
from core.kb.ingest import ingest_knowledge_base
from core.kb.store import InMemoryKnowledgeBaseStore
from core.llm import LLMProvider
from core.settings import Settings
from core.tracing import Tracing
from evals.experiments import LocalExperiments
from evals.runtime import EvalRuntime
from tests.banking_mock import NOW
from tests.conftest import BANKING_URL, FIXTURE_KB
from tests.simulation.banking_sim_mock import SimulationBankingMock


def eval_settings() -> Settings:
    return Settings(banking_api_url=BANKING_URL, kb_path=FIXTURE_KB, kb_ingest_on_startup=False, llm_profile="test")


async def fixture_kb() -> KnowledgeBaseSearch:
    kb = KnowledgeBaseSearch(store=InMemoryKnowledgeBaseStore(), embedder=HashingEmbedder())
    await ingest_knowledge_base(FIXTURE_KB, kb.store, kb.embedder, base_url="/help/")
    return kb


def make_runtime(llm: LLMProvider, mock: SimulationBankingMock, settings: Settings | None = None) -> EvalRuntime:
    return EvalRuntime(
        settings=settings or eval_settings(),
        llm=llm,
        tracing=Tracing(client=None, profile="test"),
        kb_factory=fixture_kb,
        banking_transport=mock.transport,
        clock=lambda: NOW,
    )


@dataclass
class FakeLangfuse:
    """The parts of the Langfuse client the runner and the simulation use."""

    datasets: dict[str, dict[str, Any]] = field(default_factory=dict)
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    experiments: list[dict[str, Any]] = field(default_factory=list)
    scores: list[dict[str, Any]] = field(default_factory=list)
    # What the SDK's `run_experiment` would send per item: the trace's input and metadata,
    # and each observation's input (the item task's, then each evaluator's).
    traces: list[dict[str, Any]] = field(default_factory=list)
    flushed: int = 0

    def create_dataset(self, *, name: str, **kwargs: Any) -> Any:
        self.datasets[name] = {"name": name, **kwargs}
        return SimpleNamespace(name=name)

    def create_dataset_item(self, *, dataset_name: str, id: str, **kwargs: Any) -> Any:
        self.items[id] = {"dataset_name": dataset_name, "id": id, **kwargs}
        return SimpleNamespace(id=id)

    def get_dataset(self, name: str) -> Any:
        items = [
            SimpleNamespace(
                id=i["id"],
                dataset_id=f"ds-{name}",
                input=i.get("input"),
                expected_output=i.get("expected_output"),
                metadata=i.get("metadata"),
            )
            for i in self.items.values()
            if i["dataset_name"] == name
        ]
        return SimpleNamespace(name=name, items=items)

    def run_experiment(self, **kwargs: Any) -> Any:
        """Records what the SDK traces (see `Langfuse._process_experiment_item`): the item's
        input as trace and task-observation input, the item's metadata merged with the run's
        as trace metadata, and the evaluator observations' input. Like the SDK (and the
        local backend), evaluator errors are logged and their scores dropped."""
        self.experiments.append(kwargs)
        run_metadata = dict(kwargs.get("metadata") or {})
        task = kwargs["task"]

        async def traced_task(*, item: Any, **rest: Any) -> Any:
            item_input = getattr(item, "input", None)
            meta = getattr(item, "metadata", None)
            trace = {
                "input": item_input,
                "metadata": {
                    "dataset_item_id": getattr(item, "id", None),
                    **(meta if isinstance(meta, dict) else {}),
                    **run_metadata,
                },
                "observations": [{"name": "experiment-item-task", "input": item_input}],
            }
            self.traces.append(trace)
            output = await task(item=item, **rest)
            trace["output"] = output
            return output

        def traced(evaluator: Any) -> Any:
            async def run(**inputs: Any) -> Any:
                trace = next(t for t in reversed(self.traces) if t["input"] == inputs.get("input"))
                trace["observations"].append(
                    {"name": getattr(evaluator, "__name__", "evaluator"), "as_type": "evaluator", "input": inputs}
                )
                result = evaluator(**inputs)
                return await result if hasattr(result, "__await__") else result

            return run

        result = LocalExperiments().run_experiment(
            **{**kwargs, "task": traced_task, "evaluators": [traced(e) for e in kwargs.get("evaluators", [])]}
        )
        for item_result in result.item_results:
            item_result.trace_id = uuid.uuid4().hex
        return result

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed += 1
