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
        self.experiments.append(kwargs)
        result = LocalExperiments().run_experiment(**kwargs)
        for item_result in result.item_results:
            item_result.trace_id = uuid.uuid4().hex
        return result

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed += 1
