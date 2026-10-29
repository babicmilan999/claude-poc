"""Simulate customer and Simulate email complaint (architecture sections 5 and 7).

- Simulate customer: the banking service creates an incident; a conversation is opened
  for its customer with the answer key stored hidden on it; the simulated customer then
  chats with the assistant in the background, through `AssistantService.run_turn` (the
  customer app's path). A handoff creates the case, which is investigated automatically.
- Simulate email: the incident plus a `simulated_email` case with no category.

Scoring (Langfuse, only when tracing is on): `handoff_correct` on the conversation's
session when the journey ends, `diagnosis_correct` on the investigation's trace once the
case's run settles. Scores use the answer key held here; it never goes to an agent.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.service import AssistantService
from core.enums import ConversationOutcome, IssueType
from core.jobs import JobQueue
from core.llm import LLMProvider
from evals.scorers import diagnosis_value, handoff_value
from investigation.schemas import Case
from investigation.service import InvestigationService, trace_id_for
from simulation.banking import AnswerKey, IncidentRequest, SimulationBankingClient
from simulation.cases import create_simulated_email_case
from simulation.journey import JourneyResult, run_simulated_conversation
from simulation.outcomes import is_settled, read_case_outcome
from simulation.persona import CustomerView, SimulatedCustomer
from simulation.settings import SimulationSettings

log = logging.getLogger(__name__)


class ServiceUnavailable(Exception):
    """The assistant or investigation package is not running."""


@dataclass(frozen=True)
class SimulationStarted:
    simulation_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None


def email_complaint(view: CustomerView) -> str:
    """The incident's suggested complaint, or one written from what the customer knows."""
    if view.complaint and view.complaint.strip():
        return view.complaint.strip()
    facts = "; ".join(view.persona.knows) or "something went wrong with a payment"
    return f"Hello, I have a problem with a payment: {facts}. Please look into it."


class SimulationService:
    def __init__(
        self,
        *,
        llm: LLMProvider,
        banking: SimulationBankingClient,
        settings: SimulationSettings,
        assistant: Callable[[], AssistantService | None],
        investigation: Callable[[], InvestigationService | None],
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        jobs: JobQueue | None = None,
        score_client: Any | None = None,
    ) -> None:
        self.llm = llm
        self.banking = banking
        self.settings = settings
        self._assistant = assistant
        self._investigation = investigation
        self.sessionmaker = sessionmaker
        self.jobs = jobs
        self.score_client = score_client
        self.journeys: dict[uuid.UUID, asyncio.Task[JourneyResult | None]] = {}
        self._background: set[asyncio.Task[Any]] = set()

    def _need_assistant(self) -> AssistantService:
        service = self._assistant()
        if service is None:
            raise ServiceUnavailable("the assistant is not running")
        return service

    def _need_investigation(self) -> InvestigationService:
        service = self._investigation()
        if service is None:
            raise ServiceUnavailable("investigations are not running")
        return service

    # --- Simulate customer -------------------------------------------------------------

    async def simulate_customer(self, issue_type: IssueType | None = None) -> SimulationStarted:
        assistant = self._need_assistant()
        incident = await self.banking.create_incident(IncidentRequest(type=issue_type))
        view, key = incident.customer_view(), incident.answer_key()
        conversation = await assistant.create_conversation(
            view.customer_id, simulated=True, expected_outcome=key.outcome, expected_root_cause=key.root_cause
        )
        simulation_id = uuid.uuid4()
        customer = SimulatedCustomer(self.llm, view)
        task = asyncio.create_task(
            self._journey(assistant, conversation, customer, key), name=f"simulated-customer-{simulation_id}"
        )
        self.journeys[simulation_id] = task
        task.add_done_callback(lambda _: self.journeys.pop(simulation_id, None))
        return SimulationStarted(
            simulation_id=simulation_id,
            conversation_id=conversation.id,
            customer_id=view.customer_id,
            customer_name=conversation.customer_name or view.customer_name,
        )

    async def _journey(
        self, assistant: AssistantService, conversation: Any, customer: SimulatedCustomer, key: AnswerKey
    ) -> JourneyResult | None:
        try:
            result = await run_simulated_conversation(
                assistant, conversation, customer, max_turns=self.settings.simulator_max_turns
            )
        except Exception:
            log.exception("simulated customer in conversation %s failed", conversation.id)
            with contextlib.suppress(Exception):
                await assistant.close_conversation(conversation.id, ConversationOutcome.ABANDONED)
            return None
        log.info(
            "simulated conversation %s ended: %s (%s, %d turn(s))",
            conversation.id,
            result.outcome,
            result.ended_by,
            result.turns,
        )
        self._score(
            "handoff_correct",
            handoff_value(result.handed_off, key.outcome),
            session_id=str(conversation.id),
            comment=f"{result.outcome}; expected {key.outcome.value}",
        )
        if result.case_id is not None:
            await self.score_case(result.case_id, key)
        return result

    # --- Simulate email ----------------------------------------------------------------

    async def simulate_email(self, issue_type: IssueType | None = None) -> Case:
        investigation = self._need_investigation()
        incident = await self.banking.create_incident(IncidentRequest(type=issue_type))
        view, key = incident.customer_view(), incident.answer_key()
        case = await create_simulated_email_case(
            investigation, view, key, email_complaint(view), sessionmaker=self.sessionmaker, jobs=self.jobs
        )
        if self.score_client is not None:
            self._in_background(self.score_case(case.id, key), name=f"score-case-{case.id}")
        return case

    # --- scoring -----------------------------------------------------------------------

    def _score(self, name: str, value: float, **kwargs: Any) -> None:
        if self.score_client is None:
            return
        try:
            self.score_client.create_score(name=name, value=value, **kwargs)
        except Exception:  # tracing must never break a simulation
            log.warning("could not record score %s in Langfuse", name, exc_info=True)

    async def score_case(self, case_id: uuid.UUID, key: AnswerKey) -> float | None:
        """Wait (bounded) until the case's investigation settles, then score its diagnosis
        against the answer key. Returns the score, or None if it did not settle."""
        if self.score_client is None:
            return None
        investigation = self._investigation()
        if investigation is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.simulation_score_timeout_seconds
        while True:
            outcome = await read_case_outcome(investigation.store, case_id)
            if outcome is None:
                log.info("case %s is not in the investigation store; not scored", case_id)
                return None
            if is_settled(outcome) and outcome.investigation_id:
                value = diagnosis_value(
                    outcome.diagnosis_issue_type, outcome.root_cause, key.issue_type, key.root_cause
                )
                self._score(
                    "diagnosis_correct",
                    value,
                    trace_id=trace_id_for(outcome.investigation_id),
                    comment=f"{outcome.diagnosis_issue_type}/{outcome.root_cause}",
                )
                return value
            if loop.time() >= deadline:
                log.info("case %s: no settled investigation within the timeout; not scored", case_id)
                return None
            await asyncio.sleep(self.settings.simulation_score_poll_seconds)

    def _in_background(self, coro: Any, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def wait(self) -> None:
        """Wait for running journeys and scoring (tests)."""
        while self.journeys or self._background:
            await asyncio.gather(*self.journeys.values(), *self._background, return_exceptions=True)

    async def aclose(self) -> None:
        tasks = [*self.journeys.values(), *self._background]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
