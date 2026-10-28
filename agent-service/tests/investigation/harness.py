"""A wired investigation service for unit tests: in-memory store, LangGraph's in-memory
checkpointer, the fake LLM and the banking API mocked from the contract. No workers:
tests run queued jobs with `service.run_pending()`."""

import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from core.banking import BankingClient
from core.enums import IssueType, RootCause
from core.tracing import Tracing
from core.triage import TriageOutput
from investigation.broker import Broker
from investigation.graph import GraphDeps, build_graph
from investigation.service import InvestigationService
from investigation.settings import InvestigationSettings
from investigation.state import DiagnosisOutput, EvidenceAssessment
from investigation.store_memory import MemoryInvestigationStore
from tests.banking_mock import NOW
from tests.fakes import FakeLLMProvider, structured

DEFAULT_AUTO = frozenset({IssueType.CARD_DECLINE, IssueType.MISSED_PAYMENT, IssueType.FAILED_DIRECT_DEBIT})


@dataclass
class Harness:
    service: InvestigationService
    store: Any  # MemoryInvestigationStore unless a test passes another store
    llm: FakeLLMProvider
    scores: list[tuple[str, str, float, str | None]] = field(default_factory=list)

    def events(self, investigation_id: uuid.UUID) -> list[Any]:
        return [e for e in self.store.events if e.investigation_id == investigation_id]

    def event_types(self, investigation_id: uuid.UUID) -> list[str]:
        return [str(e.type) for e in self.events(investigation_id)]

    def nodes(self, investigation_id: uuid.UUID) -> list[str]:
        return [e.payload["node"] for e in self.events(investigation_id) if e.type == "step_started"]

    def only_investigation(self, case_id: uuid.UUID) -> Any:
        rows = [i for i in self.store.investigations.values() if i.case_id == case_id]
        assert len(rows) == 1, rows
        return rows[0]

    def latest_investigation(self, case_id: uuid.UUID) -> Any:
        return max((i for i in self.store.investigations.values() if i.case_id == case_id), key=lambda i: i.seq)

    def llm_text(self) -> str:
        """Everything every model was sent (already masked)."""
        return "\n".join(call.text for alias in ("triage", "reasoning") for call in self.llm.fake(alias).calls)


def make_harness(
    llm: FakeLLMProvider,
    banking: BankingClient,
    *,
    auto_types: frozenset[IssueType] = DEFAULT_AUTO,
    store: Any = None,
    checkpointer: Any = None,
    **overrides: Any,
) -> Harness:
    store = store if store is not None else MemoryInvestigationStore()
    settings = InvestigationSettings(
        investigation_workers_enabled=False, investigation_heartbeat_seconds=60, **overrides
    )
    harness_scores: list[tuple[str, str, float, str | None]] = []

    def score(investigation_id: str, name: str, value: float, comment: str | None) -> None:
        harness_scores.append((investigation_id, name, value, comment))

    deps = GraphDeps(
        llm=llm,
        banking=banking,
        load_intake=store.load_intake,
        settings=settings,
        auto_investigate_types=auto_types,
        triage_threshold=0.6,
        clock=lambda: NOW,
        score=score,
    )
    service = InvestigationService(
        store=store,
        graph=build_graph(deps, checkpointer=checkpointer if checkpointer is not None else InMemorySaver()),
        broker=Broker(),
        settings=settings,
        tracing=Tracing(client=None, profile="test"),
    )
    return Harness(service=service, store=store, llm=llm, scores=harness_scores)


# --- scripted model outputs ----------------------------------------------------------


def triaged(category: IssueType, confidence: float = 0.9) -> Any:
    return structured(TriageOutput(category=category, confidence=confidence, reasoning="facts from the snapshot"))


def enough(*evidence_ids: str) -> Any:
    return structured(EvidenceAssessment(enough=True, evidence_ids=list(evidence_ids)))


def not_enough(missing: str = "The failure reason is not visible yet.") -> Any:
    return structured(EvidenceAssessment(enough=False, missing=missing))


def diagnosis(
    issue_type: IssueType,
    root_cause: RootCause,
    *evidence_ids: str,
    confidence: float = 0.9,
    action: str = "Tell the customer.",
) -> Any:
    return structured(
        DiagnosisOutput(
            issue_type=issue_type,
            root_cause=root_cause,
            confidence=confidence,
            evidence_ids=list(evidence_ids),
            recommended_action=action,
        )
    )
