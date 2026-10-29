"""Persistence for cases, investigations, their events, diagnoses and reviews.

`InvestigationStore` is the interface; `SqlInvestigationStore` (Postgres, production) and
`MemoryInvestigationStore` (unit tests, and runs without a database) implement it. Each
method is one atomic unit: a status change and the timeline events that announce it are
written together, so a stream that sees the status also sees the events.

Worker-side writes take the `ClaimedJob` and only apply while the job is still running
under that worker (`core.jobs.owned_by`); otherwise they raise `JobLost`.

The stores never read the simulation answer keys (`expected_root_cause`,
`expected_outcome`): `IntakeData` and the response models have no field for them.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from core.enums import CaseSource, CaseStatus, InvestigationStatus, InvestigationTrigger, IssueType
from core.jobs import ClaimedJob
from investigation.schemas import (
    Case,
    CaseDetail,
    Diagnosis,
    Escalation,
    FeedbackRequest,
    Investigation,
    InvestigationEvent,
)

EventSpec = tuple[str, dict[str, Any]]  # (type, payload) to append to the timeline


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class JobLost(Exception):
    """The job is no longer this worker's (it was requeued and claimed by another)."""


@dataclass(frozen=True)
class CaseTransition:
    """A case status (or category) change, announced on the case queue stream."""

    case_id: uuid.UUID
    previous_status: CaseStatus | None
    status: CaseStatus
    category: IssueType | None


@dataclass(frozen=True)
class NewCase:
    customer_id: uuid.UUID
    customer_name: str | None
    source: CaseSource
    complaint: str | None = None
    trigger_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class StartResult:
    investigation: Investigation
    created: bool
    transitions: list[CaseTransition] = field(default_factory=list)
    superseded_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ReviewResult:
    investigation: Investigation
    resume_payload: dict[str, Any]
    requeued: bool  # rejected: back in the queue; otherwise finalized by the API
    transitions: list[CaseTransition] = field(default_factory=list)


@dataclass(frozen=True)
class IntakeData:
    """What the graph may know about a case. Deliberately no answer keys."""

    case_id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str | None
    source: CaseSource
    category: IssueType | None
    complaint: str | None
    trigger_payload: dict[str, Any] | None
    summary: str | None
    handoff_evidence: list[dict[str, Any]]
    transcript: list[tuple[str, str]]  # (role, content), oldest first


@dataclass(frozen=True)
class RunOutcome:
    """What a run produced when it stops (paused for review or parked)."""

    issue_type: IssueType | None
    category_changed: bool
    steps_used: int
    evidence: list[dict[str, Any]]
    diagnosis: Diagnosis | None = None
    escalation: Escalation | None = None


def status_event(status: InvestigationStatus, error: str | None = None) -> EventSpec:
    return ("status_changed", {"status": status.value, "error": error})


def resume_payload_from(feedback: FeedbackRequest, review_id: uuid.UUID) -> dict[str, Any]:
    return {**feedback.model_dump(mode="json"), "review_id": str(review_id)}


class InvestigationStore(Protocol):
    # --- cases (API) ---------------------------------------------------------------------
    async def create_case(self, new: NewCase) -> Case:
        """Insert the case and a queued `auto` investigation in one transaction."""
        ...

    async def list_cases(
        self,
        *,
        statuses: list[CaseStatus],
        source: CaseSource | None,
        category: IssueType | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Case], int]: ...

    async def get_case(self, case_id: uuid.UUID) -> Case | None: ...

    async def get_case_detail(self, case_id: uuid.UUID) -> CaseDetail | None: ...

    async def start_investigation(self, case_id: uuid.UUID, *, reinvestigate: bool) -> StartResult:
        """Investigate (returns the active one if any) or Re-investigate (supersedes one
        awaiting review; Conflict while queued or running). Conflict on a resolved case,
        NotFound for an unknown one. New runs are `manual`."""
        ...

    # --- investigations (API) --------------------------------------------------------------
    async def get_investigation(self, investigation_id: uuid.UUID) -> Investigation | None: ...

    async def investigation_status(self, investigation_id: uuid.UUID) -> InvestigationStatus | None: ...

    async def record_review(self, investigation_id: uuid.UUID, feedback: FeedbackRequest) -> ReviewResult:
        """Only while `awaiting_review` (Conflict otherwise). Accepted / corrected complete
        the investigation and resolve the case; rejected requeues it with the review as
        its resume payload and moves the case back to `investigating`."""
        ...

    # --- events -----------------------------------------------------------------------------
    async def append_event(
        self, investigation_id: uuid.UUID, type: str, payload: dict[str, Any], *, job: ClaimedJob | None = None
    ) -> InvestigationEvent:
        """With `job`: only while that worker still owns it (`JobLost` otherwise), in the
        same transaction as the insert. Without: unconditional (API-side writes)."""
        ...

    async def list_events(
        self, investigation_id: uuid.UUID, after_id: int | None = None
    ) -> list[InvestigationEvent]: ...

    # --- worker: the job queue ---------------------------------------------------------------
    async def claim_next(self, worker_id: str) -> ClaimedJob | None: ...

    async def heartbeat(self, investigation_id: uuid.UUID, worker_id: str) -> bool: ...

    async def release_for_retry(
        self, job: ClaimedJob, error: str, *, max_attempts: int
    ) -> InvestigationStatus | None: ...

    async def requeue_stale(
        self, *, stale_after: timedelta, max_attempts: int
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]: ...

    # --- worker: the run -------------------------------------------------------------------
    async def load_intake(self, case_id: uuid.UUID) -> IntakeData: ...

    async def mark_started(self, job: ClaimedJob, events: list[EventSpec]) -> list[CaseTransition]:
        """After the claim: case → `investigating` (from new, needs_manual, escalated, ...)."""
        ...

    async def record_classification(
        self, job: ClaimedJob, issue_type: IssueType, category_changed: bool | None = None
    ) -> list[CaseTransition]:
        """Set the investigation's issue type (and `category_changed` unless None) and the
        case's category."""
        ...

    async def pause_for_review(
        self, job: ClaimedJob, outcome: RunOutcome, events: list[EventSpec]
    ) -> list[CaseTransition]:
        """Store the diagnosis or escalation, investigation → `awaiting_review`, case →
        `awaiting_review` (diagnosis) or `escalated`."""
        ...

    async def complete_parked(
        self, job: ClaimedJob, outcome: RunOutcome, events: list[EventSpec]
    ) -> list[CaseTransition]:
        """Investigation → `completed` with no diagnosis, case → `needs_manual`."""
        ...

    async def fail_cases(self, investigation_ids: list[uuid.UUID], error: str) -> list[CaseTransition]:
        """For investigations that ended `failed`: case → `needs_manual`, plus the
        status_changed event on each timeline."""
        ...


def trigger_of(job: ClaimedJob) -> InvestigationTrigger:
    return InvestigationTrigger(job.trigger)
