"""Postgres job queue for investigations (architecture section 5).

The `queued` rows of `agents.investigations` are the queue; there is no broker.

- `enqueue()` inserts a queued investigation in the caller's transaction and sends
  `NOTIFY investigation_jobs`; Postgres delivers the notification only on commit, so the
  case, the investigation and the wake-up are atomic.
- `claim_next()` takes the oldest queued job with `SELECT ... FOR UPDATE SKIP LOCKED`, so
  two workers never take the same job.
- A worker that dies leaves its job `running` with an old `heartbeat_at`;
  `requeue_stale()` puts it back (LangGraph resumes from the last checkpoint) or fails it
  after `max_attempts`.
- `NotificationListener` wakes idle workers on `NOTIFY`; workers also poll, so a missed
  notification only delays a job.
- Every write by a worker after the claim (`heartbeat`, `release_for_retry`, `finish`)
  only applies while the job is still `running` under that worker's `worker_id`: once
  `requeue_stale()` has handed a slow worker's job to another worker, the slow worker can
  no longer change it. They report whether they did; `owned_by(job)` gives the same
  condition for updates the investigation package makes in its own transactions.

Case status changes (new → investigating, ...) belong to the investigation package.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, Update, and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Investigation
from core.enums import InvestigationStatus, InvestigationTrigger

log = logging.getLogger(__name__)

INVESTIGATION_CHANNEL = "investigation_jobs"
CASE_EVENTS_CHANNEL = "case_events"


@dataclass(frozen=True)
class ClaimedJob:
    investigation_id: uuid.UUID
    case_id: uuid.UUID
    trigger: str
    attempts: int
    resume_payload: dict[str, Any] | None
    worker_id: str


async def notify(session: AsyncSession, channel: str, payload: str = "") -> None:
    """`NOTIFY` inside the session's transaction (delivered on commit)."""
    await session.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": payload})


def owned_by(job: ClaimedJob) -> ColumnElement[bool]:
    """The job is still running under the worker that claimed it."""
    return and_(
        Investigation.id == job.investigation_id,
        Investigation.worker_id == job.worker_id,
        Investigation.status == InvestigationStatus.RUNNING.value,
    )


def release_statement(job: ClaimedJob, error: str, *, retry: bool) -> Update:
    status = InvestigationStatus.QUEUED if retry else InvestigationStatus.FAILED
    return (
        update(Investigation)
        .where(owned_by(job))
        .values(
            status=status.value,
            worker_id=None,
            error=error[:2000],
            finished_at=None if retry else func.now(),
        )
    )


_TERMINAL = frozenset({InvestigationStatus.COMPLETED, InvestigationStatus.FAILED, InvestigationStatus.SUPERSEDED})


def finish_statement(job: ClaimedJob, status: InvestigationStatus, error: str | None = None) -> Update:
    """The worker hands the job off: a final status, or `awaiting_review` (paused at a
    human review; a review decision queues it again with `requeue`)."""
    if status in (InvestigationStatus.QUEUED, InvestigationStatus.RUNNING):
        raise ValueError(f"finish() cannot set {status.value}; use release_for_retry or requeue")
    return (
        update(Investigation)
        .where(owned_by(job))
        .values(
            status=status.value,
            worker_id=None,
            error=error[:2000] if error else None,
            finished_at=func.now() if status in _TERMINAL else None,
        )
    )


def claim_statement(worker_id: str) -> Update:
    next_job = (
        select(Investigation.id)
        .where(Investigation.status == InvestigationStatus.QUEUED.value)
        .order_by(Investigation.queued_at, Investigation.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (
        update(Investigation)
        .where(Investigation.id == next_job.scalar_subquery())
        .values(
            status=InvestigationStatus.RUNNING.value,
            attempts=Investigation.attempts + 1,
            worker_id=worker_id,
            started_at=func.coalesce(Investigation.started_at, func.now()),
            heartbeat_at=func.now(),
        )
        .returning(
            Investigation.id,
            Investigation.case_id,
            Investigation.trigger,
            Investigation.attempts,
            Investigation.resume_payload,
        )
    )


class JobQueue:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], channel: str = INVESTIGATION_CHANNEL) -> None:
        self._sessionmaker = sessionmaker
        self.channel = channel

    async def enqueue(
        self,
        session: AsyncSession,
        case_id: uuid.UUID,
        *,
        trigger: InvestigationTrigger = InvestigationTrigger.AUTO,
    ) -> Investigation:
        """Add a queued investigation in the caller's transaction (the caller commits).
        Raises IntegrityError if the case already has an active investigation."""
        investigation = Investigation(
            id=uuid.uuid4(),
            case_id=case_id,
            trigger=trigger.value,
            status=InvestigationStatus.QUEUED.value,
        )
        session.add(investigation)
        await session.flush()
        await notify(session, self.channel, str(investigation.id))
        return investigation

    async def requeue(self, session: AsyncSession, investigation_id: uuid.UUID, resume_payload: dict[str, Any]) -> None:
        """Queue an existing investigation again with a payload to resume with (for
        example a reject-with-hint review), in the caller's transaction."""
        await session.execute(
            update(Investigation)
            .where(Investigation.id == investigation_id)
            .values(status=InvestigationStatus.QUEUED.value, resume_payload=resume_payload, worker_id=None)
        )
        await notify(session, self.channel, str(investigation_id))

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        async with self._sessionmaker.begin() as session:
            row = (await session.execute(claim_statement(worker_id))).first()
        if row is None:
            return None
        return ClaimedJob(
            investigation_id=row.id,
            case_id=row.case_id,
            trigger=row.trigger,
            attempts=row.attempts,
            resume_payload=row.resume_payload,
            worker_id=worker_id,
        )

    async def heartbeat(self, investigation_id: uuid.UUID, worker_id: str) -> bool:
        """Returns False if the job is no longer this worker's."""
        async with self._sessionmaker.begin() as session:
            result = await session.execute(
                update(Investigation)
                .where(
                    and_(
                        Investigation.id == investigation_id,
                        Investigation.worker_id == worker_id,
                        Investigation.status == InvestigationStatus.RUNNING.value,
                    )
                )
                .values(heartbeat_at=func.now())
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def release_for_retry(self, job: ClaimedJob, error: str, *, max_attempts: int) -> InvestigationStatus | None:
        """After a failed run: back to `queued`, or `failed` once attempts are used up.
        Returns the new status, or None if the job is no longer this worker's (nothing
        changed)."""
        retry = job.attempts < max_attempts
        status = InvestigationStatus.QUEUED if retry else InvestigationStatus.FAILED
        async with self._sessionmaker.begin() as session:
            result = await session.execute(release_statement(job, error, retry=retry))
            if not result.rowcount:  # type: ignore[attr-defined]
                log.warning(
                    "investigation %s: not released, no longer owned by worker %s", job.investigation_id, job.worker_id
                )
                return None
            if retry:
                await notify(session, self.channel, str(job.investigation_id))
        return status

    async def finish(
        self,
        job: ClaimedJob,
        status: InvestigationStatus,
        *,
        error: str | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """Set a final status (`completed`, `awaiting_review`, `failed`, ...) if the job is
        still this worker's. With `session`, runs in the caller's transaction (the caller
        commits). Returns whether the job was updated."""
        if session is not None:
            result = await session.execute(finish_statement(job, status, error))
            return bool(result.rowcount)  # type: ignore[attr-defined]
        async with self._sessionmaker.begin() as own:
            result = await own.execute(finish_statement(job, status, error))
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def requeue_stale(
        self, *, stale_after: timedelta, max_attempts: int
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Recover jobs of dead workers. Returns (requeued ids, failed ids)."""
        cutoff = func.now() - stale_after
        stale = and_(
            Investigation.status == InvestigationStatus.RUNNING.value,
            Investigation.heartbeat_at < cutoff,
        )
        async with self._sessionmaker.begin() as session:
            requeued = (
                (
                    await session.execute(
                        update(Investigation)
                        .where(and_(stale, Investigation.attempts < max_attempts))
                        .values(status=InvestigationStatus.QUEUED.value, worker_id=None)
                        .returning(Investigation.id)
                    )
                )
                .scalars()
                .all()
            )
            failed = (
                (
                    await session.execute(
                        update(Investigation)
                        .where(and_(stale, Investigation.attempts >= max_attempts))
                        .values(
                            status=InvestigationStatus.FAILED.value,
                            worker_id=None,
                            error="worker stopped responding; retries used up",
                            finished_at=func.now(),
                        )
                        .returning(Investigation.id)
                    )
                )
                .scalars()
                .all()
            )
            if requeued:
                await notify(session, self.channel, "requeued")
        return list(requeued), list(failed)


class NotificationListener:
    """`LISTEN` on one dedicated connection; `wait()` returns when a notification arrives
    or the timeout passes."""

    def __init__(self, database_url: str, channels: list[str]) -> None:
        self._database_url = database_url
        self._channels = channels
        self._event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._conn: Any = None

    async def start(self) -> None:
        import psycopg

        self._conn = await psycopg.AsyncConnection.connect(self._database_url, autocommit=True)
        for channel in self._channels:
            await self._conn.execute(f'LISTEN "{channel}"')
        self._task = asyncio.create_task(self._read(), name="pg-listener")

    async def _read(self) -> None:
        try:
            async for _ in self._conn.notifies():
                self._event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("notification listener stopped; workers fall back to polling")

    async def wait(self, timeout: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout)
        notified = self._event.is_set()
        self._event.clear()
        return notified

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._conn is not None:
            await self._conn.close()


JobHandler = Callable[[ClaimedJob], Awaitable[None]]


async def run_worker(
    queue: JobQueue,
    handler: JobHandler,
    *,
    stop: asyncio.Event,
    worker_id: str,
    max_attempts: int = 3,
    poll_interval: float = 5.0,
    wait: Callable[[float], Awaitable[bool]] | None = None,
) -> None:
    """Generic worker loop: claim, handle, repeat; sleep until NOTIFY or the poll interval
    when the queue is empty. The handler owns the job's final status; if it raises, the
    job is released for retry (or failed after `max_attempts`)."""

    async def _idle(timeout: float) -> bool:
        if wait is not None:
            return await wait(timeout)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout)
        return False

    while not stop.is_set():
        try:
            job = await queue.claim_next(worker_id)
        except Exception:
            log.exception("worker %s: claiming a job failed", worker_id)
            await _idle(poll_interval)
            continue
        if job is None:
            await _idle(poll_interval)
            continue
        try:
            await handler(job)
        except Exception as exc:
            log.exception("worker %s: investigation %s failed", worker_id, job.investigation_id)
            await queue.release_for_retry(job, f"{exc.__class__.__name__}: {exc}", max_attempts=max_attempts)
