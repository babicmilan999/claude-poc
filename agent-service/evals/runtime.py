"""What an eval item runs on, and the item task itself.

Each item gets a fresh in-process world (architecture section 10: "runs the graph
in-process"): the assistant and the investigation graph with in-memory stores and
LangGraph's in-memory checkpointer, a banking client and the banking simulation client.
Shared across items: the LLM provider (real aliases through LiteLLM, or the fake LLM in
tests), the knowledge base and tracing.

The task gets only the public scenario (no `expected` block), looked up by the runner
from the item's opaque id: the scenario never travels as a dataset item or trace input. It creates the incident
through the banking simulation, which returns its own answer key; that key is only
handed to the stores' `simulated` / `expected_*` parameters, exactly like the Simulate
buttons do, and never to an agent.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from assistant.graph import AssistantDeps, build_assistant_graph
from assistant.service import AssistantService
from assistant.store import InMemoryConversationStore
from core.banking import BankingClient
from core.kb import KnowledgeBaseSearch
from core.llm import LLMProvider
from core.settings import Settings
from core.tools.common import Clock, utc_now
from core.tracing import Tracing
from evals.results import AnswerOut, ConversationOut, ItemOutput, Mode
from evals.scenario import PublicScenario
from investigation.broker import Broker
from investigation.graph import GraphDeps, build_graph
from investigation.service import InvestigationService
from investigation.settings import InvestigationSettings
from investigation.store_memory import MemoryInvestigationStore
from simulation.banking import IncidentRequest, SimulationBankingClient
from simulation.cases import create_simulated_email_case, investigation_handoff_sink
from simulation.journey import JourneyResult, run_scripted_conversation, run_simulated_conversation
from simulation.outcomes import read_case_outcome
from simulation.persona import CustomerView, PersonaSpec, SimulatedCustomer
from simulation.service import email_complaint
from simulation.settings import SimulationSettings

KbFactory = Callable[[], Awaitable[KnowledgeBaseSearch | None]]


def default_kb_factory(settings: Settings) -> KbFactory:
    """The knowledge base in memory, embedded with the configured model (no database)."""

    async def build() -> KnowledgeBaseSearch | None:
        if not settings.kb_path.is_dir():
            return None
        from core.kb.embeddings import FastEmbedEmbedder
        from core.kb.ingest import ingest_knowledge_base
        from core.kb.store import InMemoryKnowledgeBaseStore

        kb = KnowledgeBaseSearch(
            store=InMemoryKnowledgeBaseStore(), embedder=FastEmbedEmbedder(settings.embedding_model)
        )
        await ingest_knowledge_base(settings.kb_path, kb.store, kb.embedder, base_url=settings.kb_article_base_url)
        return kb

    return build


@dataclass
class EvalRuntime:
    settings: Settings
    llm: LLMProvider
    tracing: Tracing
    kb_factory: KbFactory | None = None
    banking_transport: httpx.AsyncBaseTransport | None = None  # tests: the contract mock
    clock: Clock = utc_now
    investigation_settings: InvestigationSettings = field(
        default_factory=lambda: InvestigationSettings(investigation_workers_enabled=False)
    )
    simulation_settings: SimulationSettings = field(default_factory=SimulationSettings)
    _kb: KnowledgeBaseSearch | None = field(default=None, init=False, repr=False)
    _kb_ready: bool = field(default=False, init=False, repr=False)
    _kb_lock: asyncio.Lock | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> "EvalRuntime":
        from core.llm import LiteLLMProvider
        from evals.redaction import init_eval_langfuse

        return cls(
            settings=settings,
            llm=LiteLLMProvider(settings),
            tracing=Tracing(client=init_eval_langfuse(settings), profile=settings.llm_profile),
            kb_factory=default_kb_factory(settings),
        )

    async def knowledge_base(self) -> KnowledgeBaseSearch | None:
        if self._kb_ready:
            return self._kb
        if self._kb_lock is None:
            self._kb_lock = asyncio.Lock()
        async with self._kb_lock:
            if not self._kb_ready:
                self._kb = await self.kb_factory() if self.kb_factory is not None else None
                self._kb_ready = True
        return self._kb


class ItemWorld:
    """A fresh assistant + investigation setup for one item (async context manager)."""

    def __init__(self, runtime: EvalRuntime, *, with_kb: bool = True) -> None:
        self.runtime = runtime
        self.with_kb = with_kb
        settings = runtime.settings
        self.banking = BankingClient(
            settings.banking_api_url, timeout=settings.banking_timeout_seconds, transport=runtime.banking_transport
        )
        self.simulation = SimulationBankingClient(
            settings.banking_api_url,
            timeout=runtime.simulation_settings.simulation_banking_timeout_seconds,
            transport=runtime.banking_transport,
        )
        self.conversations = InMemoryConversationStore(clock=runtime.clock)
        self.cases = MemoryInvestigationStore(clock=runtime.clock)
        self.assistant: AssistantService | None = None
        self.investigation: InvestigationService | None = None

    async def __aenter__(self) -> Self:
        runtime, settings = self.runtime, self.runtime.settings
        kb = await runtime.knowledge_base() if self.with_kb else None  # only the assistant searches it
        deps = AssistantDeps(
            llm=runtime.llm,
            banking=self.banking,
            kb=kb,
            store=self.conversations,
            triage_threshold=settings.triage_confidence_threshold,
            clock=runtime.clock,
        )
        self.assistant = AssistantService(
            graph=build_assistant_graph(deps, InMemorySaver()),
            store=self.conversations,
            banking=self.banking,
            tracing=runtime.tracing,
            clock=runtime.clock,
        )
        graph_deps = GraphDeps(
            llm=runtime.llm,
            banking=self.banking,
            load_intake=self.cases.load_intake,
            settings=runtime.investigation_settings,
            auto_investigate_types=frozenset(settings.auto_investigate_types),
            triage_threshold=settings.triage_confidence_threshold,
            clock=runtime.clock,
        )
        self.investigation = InvestigationService(
            store=self.cases,
            graph=build_graph(graph_deps, checkpointer=InMemorySaver()),
            broker=Broker(),
            settings=runtime.investigation_settings,
            tracing=runtime.tracing,
        )
        # Handoff cases go straight to the investigation's store, as in the app without a database.
        self.conversations.handoff_sink = investigation_handoff_sink(self.investigation)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self.assistant is not None:
            await self.assistant.aclose()
        if self.investigation is not None:
            await self.investigation.wait_background()
        await self.banking.aclose()
        await self.simulation.aclose()


def persona_for(view: CustomerView, scenario: PublicScenario) -> CustomerView:
    """The scenario's persona (reviewed, repeatable style) with the incident's facts: the
    banking service knows the real merchant, amounts and names it set up."""
    knows = view.persona.knows or scenario.persona.knows
    persona = PersonaSpec.model_validate({**scenario.persona.model_dump(), "knows": knows})
    return view.model_copy(update={"persona": persona})


def _conversation_out(result: JourneyResult, category: object) -> ConversationOut:
    return ConversationOut(
        conversation_id=str(result.conversation_id),
        outcome=result.outcome,
        ended_by=result.ended_by,
        category=category,  # type: ignore[arg-type]
        turns=result.turns,
        case_id=str(result.case_id) if result.case_id else None,
        errors=result.errors,
        transcript=[{"role": line.role, "text": line.text} for line in result.lines],
    )


async def _investigate(world: ItemWorld, case_id: uuid.UUID, output: ItemOutput, provided: object) -> None:
    assert world.investigation is not None
    started = time.perf_counter()
    await world.investigation.run_pending(worker_id="eval")
    outcome = await read_case_outcome(world.cases, case_id, provided_category=provided)  # type: ignore[arg-type]
    if outcome is not None:
        if outcome.root_cause is not None:
            outcome.seconds_to_diagnosis = round(time.perf_counter() - started, 3)
        output.case = outcome


async def run_item(runtime: EvalRuntime, scenario: PublicScenario, mode: Mode) -> ItemOutput:
    """Run one scenario in one mode. Never raises: an error ends up in `output.error`."""
    started = time.perf_counter()
    output = ItemOutput(mode=mode)
    try:
        async with ItemWorld(runtime, with_kb=mode is not Mode.COPILOT) as world:
            assert world.assistant is not None and world.investigation is not None
            incident = await world.simulation.create_incident(
                IncidentRequest(
                    type=scenario.incident.type,
                    root_cause=scenario.incident.root_cause,
                    params=scenario.incident.params,
                )
            )
            view, key = persona_for(incident.customer_view(), scenario), incident.answer_key()

            if mode is Mode.COPILOT:
                complaint = scenario.complaint or email_complaint(view)
                case = await create_simulated_email_case(
                    world.investigation, view, key, complaint, sessionmaker=None, jobs=None
                )
                await _investigate(world, case.id, output, None)
            else:
                conversation = await world.assistant.create_conversation(
                    view.customer_id, simulated=True, expected_outcome=key.outcome, expected_root_cause=key.root_cause
                )
                if mode is Mode.ASSISTANT:
                    result = await run_scripted_conversation(
                        world.assistant, conversation, scenario.conversation_script or [], capture_answers=True
                    )
                else:
                    result = await run_simulated_conversation(
                        world.assistant,
                        conversation,
                        SimulatedCustomer(runtime.llm, view),
                        max_turns=runtime.simulation_settings.simulator_max_turns,
                        capture_answers=True,
                    )
                record = await world.conversations.get_conversation(conversation.id)
                category = record.category if record else None
                output.conversation = _conversation_out(result, category)
                output.answers = [AnswerOut(text=a.text, sources=a.sources) for a in result.answers]
                if mode is Mode.JOURNEY and result.handed_off and result.case_id is not None:
                    await _investigate(world, result.case_id, output, category)
    except Exception as exc:
        output.error = f"{exc.__class__.__name__}: {exc}"
    output.seconds = time.perf_counter() - started
    return output
