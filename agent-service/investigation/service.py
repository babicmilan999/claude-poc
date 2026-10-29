"""Runs investigations and applies reviews: the glue between the store, the graph, the
job queue and the live streams.

- `handle_job` is the worker handler: it runs (or resumes) the graph for a claimed job,
  then stores where the run stopped (paused at review, or parked). Failures are released
  for retry; a job still failing after its attempts ends `failed` and its case goes to
  `needs_manual`.
- Every timeline event the graph emits is stored (masked) and wakes the SSE streams.
- `submit_feedback` records the review. Rejected: the same investigation is queued again
  with the review as its resume payload (same LangGraph thread). Accepted / corrected:
  the investigation completes and the case resolves at once; the paused thread is then
  resumed to `finalize` (Langfuse scores) in the background; its timeline step is
  written with the review, so nothing is appended after the stream has closed.
"""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from core.enums import InvestigationStatus, IssueType
from core.jobs import ClaimedJob
from core.masking import mask_data
from core.tracing import Tracing
from investigation import prompts
from investigation.broker import Broker
from investigation.common import Emit
from investigation.schemas import (
    Case,
    CaseCreatedEvent,
    CaseStatusChangedEvent,
    Diagnosis,
    Escalation,
    FeedbackRequest,
)
from investigation.settings import InvestigationSettings
from investigation.store import (
    CaseTransition,
    InvestigationStore,
    JobLost,
    NewCase,
    ReviewResult,
    RunOutcome,
    StartResult,
    status_event,
)

log = logging.getLogger(__name__)

RECURSION_LIMIT = 200
ORIGIN = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"  # this process, for cross-process case events

CaseNotifier = Callable[[dict[str, Any]], Any]


def trace_id_for(investigation_id: uuid.UUID | str) -> str:
    """A Langfuse trace id derived from the investigation id, so every run and resume of
    one investigation lands in the same trace and finalize can score it."""
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=str(investigation_id))


class InvestigationService:
    def __init__(
        self,
        *,
        store: InvestigationStore,
        graph: CompiledStateGraph,
        broker: Broker,
        settings: InvestigationSettings,
        tracing: Tracing,
        case_notifier: CaseNotifier | None = None,
    ) -> None:
        self.store = store
        self.graph = graph
        self.broker = broker
        self.settings = settings
        self.tracing = tracing
        self.case_notifier = case_notifier
        self._background: set[asyncio.Task[Any]] = set()

    # --- case queue events -------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _event_id(self) -> str:
        return uuid.uuid4().hex

    def publish_case_event(self, event: dict[str, Any], *, notify: bool = True) -> None:
        self.broker.publish_case(event)
        if notify and self.case_notifier is not None:
            try:
                self.case_notifier({"origin": ORIGIN, "event": event})
            except Exception:
                log.warning("case event notify failed", exc_info=True)

    def publish_case_created(self, case: Case, *, notify: bool = True) -> None:
        event = CaseCreatedEvent(id=self._event_id(), created_at=self._now(), case=case)
        self.publish_case_event(event.model_dump(mode="json"), notify=notify)

    def publish_transitions(self, transitions: list[CaseTransition]) -> None:
        for t in transitions:
            event = CaseStatusChangedEvent(
                id=self._event_id(),
                created_at=self._now(),
                case_id=t.case_id,
                previous_status=t.previous_status,
                status=t.status,
                category=t.category,
            ).model_dump(mode="json")
            if event.get("previous_status") is None:
                event.pop("previous_status", None)  # not nullable in the contract
            self.publish_case_event(event)

    # --- API operations ---------------------------------------------------------------------

    async def create_case(self, new: NewCase) -> Case:
        case = await self.store.create_case(new)
        self.publish_case_created(case)
        return case

    async def start_investigation(self, case_id: uuid.UUID, *, reinvestigate: bool) -> StartResult:
        result = await self.store.start_investigation(case_id, reinvestigate=reinvestigate)
        if result.superseded_id is not None:
            self.broker.publish_investigation(result.superseded_id)
        self.publish_transitions(result.transitions)
        return result

    async def submit_feedback(self, investigation_id: uuid.UUID, feedback: FeedbackRequest) -> ReviewResult:
        result = await self.store.record_review(investigation_id, feedback)
        self.broker.publish_investigation(investigation_id)
        self.publish_transitions(result.transitions)
        return result

    async def finalize(self, investigation_id: uuid.UUID, resume_payload: dict[str, Any]) -> None:
        """Resume a reviewed (accepted / corrected) thread so it runs `finalize`. The
        outcome is already stored; this only completes the thread and scores it."""
        # The review already wrote this run's last events (`step_started finalize`, then
        # `completed`, which closes the streams): nothing the resumed thread emits is stored.
        config = self._config(investigation_id, _discard)
        try:
            snapshot = await self.graph.aget_state(config)
            if not snapshot.interrupts:
                log.info("investigation %s: no paused review to finalize", investigation_id)
                return
            if snapshot.values.get("last_review_id") == resume_payload.get("review_id"):
                return
            await self.graph.ainvoke(Command(resume=resume_payload), config)
        except Exception:
            log.exception("investigation %s: finalize failed (the review is stored)", investigation_id)
        finally:
            self.tracing.flush()

    def finalize_in_background(self, investigation_id: uuid.UUID, resume_payload: dict[str, Any]) -> None:
        task = asyncio.create_task(self.finalize(investigation_id, resume_payload), name=f"finalize-{investigation_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def wait_background(self) -> None:
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # --- running a job -----------------------------------------------------------------------

    def _emitter(self, job: ClaimedJob) -> Emit:
        """Stores the run's events while `job` is still this worker's (`JobLost` stops a
        run whose job was handed to another worker)."""
        investigation_id = job.investigation_id

        async def sink(type_: str, payload: dict[str, Any]) -> None:
            masked = mask_data(payload)
            if type_ == "classified":
                transitions = await self.store.record_classification(job, IssueType(masked["issue_type"]))
                self.publish_transitions(transitions)
            await self.store.append_event(investigation_id, type_, masked, job=job)
            self.broker.publish_investigation(investigation_id)

        return sink

    def _config(self, investigation_id: uuid.UUID, emit: Emit) -> RunnableConfig:
        config = self.tracing.config(
            run_name="investigation",
            session_id=str(investigation_id),
            tags=["investigation"],
            metadata={"investigation_id": str(investigation_id), "prompt_version": prompts.PROMPT_VERSION},
        )
        if self.tracing.enabled:
            from langfuse.langchain import CallbackHandler

            config["callbacks"] = [CallbackHandler(trace_context={"trace_id": trace_id_for(investigation_id)})]
        config["configurable"] = {"thread_id": str(investigation_id), "emit": emit}
        config["recursion_limit"] = RECURSION_LIMIT
        return config

    def score(self, investigation_id: str, name: str, value: float, comment: str | None) -> None:
        """Langfuse score on the investigation's trace (no-op without Langfuse)."""
        client = self.tracing.client
        if client is None or not investigation_id:
            return
        client.create_score(name=name, value=value, trace_id=trace_id_for(investigation_id), comment=comment)

    async def run_job(self, job: ClaimedJob) -> None:
        investigation_id = job.investigation_id
        config = self._config(investigation_id, self._emitter(job))
        transitions = await self.store.mark_started(job, [status_event(InvestigationStatus.RUNNING)])
        self.broker.publish_investigation(investigation_id)
        self.publish_transitions(transitions)

        snapshot = await self.graph.aget_state(config)
        payload = job.resume_payload
        if snapshot.interrupts:
            # Paused at review. Resume with a review not applied yet; otherwise the run
            # already reached this review (a retry after a crash) and only needs storing.
            if payload and payload.get("review_id") != snapshot.values.get("last_review_id"):
                await self.graph.ainvoke(Command(resume=payload), config)
        elif snapshot.next:
            await self.graph.ainvoke(None, config)  # a retry: continue from the last checkpoint
        elif not snapshot.values:
            initial = {
                "investigation_id": str(investigation_id),
                "case_id": str(job.case_id),
                "trigger": job.trigger,
            }
            await self.graph.ainvoke(initial, config)
        self.tracing.flush()

        snapshot = await self.graph.aget_state(config)
        values = snapshot.values
        outcome = RunOutcome(
            issue_type=IssueType(values["issue_type"]) if values.get("issue_type") else None,
            category_changed=bool(values.get("category_changed")),
            steps_used=int(values.get("steps_used", 0)),
            evidence=list(values.get("evidence", [])),
            diagnosis=Diagnosis.model_validate(values["diagnosis"]) if values.get("diagnosis") else None,
            escalation=Escalation.model_validate(values["escalation"]) if values.get("escalation") else None,
        )
        if snapshot.interrupts:
            events = [
                ("awaiting_review", {"has_diagnosis": outcome.diagnosis is not None}),
                status_event(InvestigationStatus.AWAITING_REVIEW),
            ]
            transitions = await self.store.pause_for_review(job, outcome, events)
        elif values.get("outcome") == "parked":
            transitions = await self.store.complete_parked(job, outcome, [status_event(InvestigationStatus.COMPLETED)])
        else:
            raise RuntimeError(f"run stopped at {snapshot.next or 'the end'} without a review or a park")
        self.broker.publish_investigation(investigation_id)
        self.publish_transitions(transitions)

    async def handle_job(self, job: ClaimedJob) -> None:
        """Worker handler: never raises (the job's final status is handled here)."""
        run = asyncio.create_task(self.run_job(job), name=f"investigation-{job.investigation_id}")
        lost = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(job, run, lost), name=f"heartbeat-{job.investigation_id}")
        try:
            await run
        except JobLost:
            log.warning("investigation %s: job taken over by another worker", job.investigation_id)
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            log.warning("investigation %s: heartbeat lost, run stopped", job.investigation_id)
        except Exception as exc:
            log.exception("investigation %s: run failed (attempt %s)", job.investigation_id, job.attempts)
            await self._release(job, f"{exc.__class__.__name__}: {exc}")
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _release(self, job: ClaimedJob, error: str) -> None:
        status = await self.store.release_for_retry(job, error, max_attempts=self.settings.investigation_max_attempts)
        if status is InvestigationStatus.FAILED:
            self.publish_transitions(await self.store.fail_cases([job.investigation_id], error))
            self.broker.publish_investigation(job.investigation_id)

    async def _heartbeat(self, job: ClaimedJob, run: asyncio.Task[None], lost: asyncio.Event) -> None:
        while not run.done():
            await asyncio.sleep(self.settings.investigation_heartbeat_seconds)
            try:
                still_ours = await self.store.heartbeat(job.investigation_id, job.worker_id)
            except Exception:
                log.warning("investigation %s: heartbeat failed", job.investigation_id, exc_info=True)
                continue
            if not still_ours:
                lost.set()
                run.cancel()
                return

    async def run_pending(self, worker_id: str = "inline") -> int:
        """Claim and run queued jobs until the queue is empty (tests, and runs without
        the worker pool). Returns how many jobs ran."""
        count = 0
        while (job := await self.store.claim_next(worker_id)) is not None:
            await self.handle_job(job)
            count += 1
        return count

    async def recover_stale(self) -> None:
        stale_after = timedelta(seconds=self.settings.investigation_stale_after_seconds)
        requeued, failed = await self.store.requeue_stale(
            stale_after=stale_after, max_attempts=self.settings.investigation_max_attempts
        )
        if requeued:
            log.info("requeued %d investigation(s) of stopped workers", len(requeued))
        if failed:
            self.publish_transitions(await self.store.fail_cases(failed, "worker stopped responding; retries used up"))
            for investigation_id in failed:
                self.broker.publish_investigation(investigation_id)


async def _discard(type_: str, payload: dict[str, Any]) -> None:
    return None


def case_event_from_notification(payload: str) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Parse a `case_events` NOTIFY payload: `{"origin", "event"}` from this package, or
    `{"type": "case_created", "case_id"}` from another one (for example the assistant's
    handoff). Returns (origin, event, case_id_to_load)."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    if isinstance(data.get("event"), dict):
        return data.get("origin"), data["event"], None
    if data.get("type") == "case_created" and data.get("case_id"):
        return data.get("origin"), None, str(data["case_id"])
    return None, None, None
