"""Worker loop logic, without Postgres (the queue itself is tested in
`test_integration_postgres.py`)."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from core.enums import InvestigationStatus
from core.jobs import ClaimedJob, JobQueue, finish_statement, release_statement, run_worker


class FakeQueue:
    def __init__(self, jobs: list[ClaimedJob]) -> None:
        self.jobs = list(jobs)
        self.released: list[tuple[ClaimedJob, str, int]] = []
        self.claims = 0

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        self.claims += 1
        return self.jobs.pop(0) if self.jobs else None

    async def release_for_retry(self, job: ClaimedJob, error: str, *, max_attempts: int) -> InvestigationStatus | None:
        self.released.append((job, error, max_attempts))
        return InvestigationStatus.QUEUED


def _job(attempts: int = 1, worker_id: str = "w1") -> ClaimedJob:
    return ClaimedJob(
        investigation_id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        trigger="auto",
        attempts=attempts,
        resume_payload=None,
        worker_id=worker_id,
    )


async def test_worker_handles_jobs_and_releases_failures() -> None:
    ok, bad = _job(), _job()
    queue = FakeQueue([ok, bad])
    handled: list[ClaimedJob] = []
    stop = asyncio.Event()

    async def handler(job: ClaimedJob) -> None:
        handled.append(job)
        if job is bad:
            raise RuntimeError("LLM timeout")

    async def wait(timeout: float) -> bool:
        stop.set()  # queue empty: stop the loop
        return False

    await asyncio.wait_for(
        run_worker(queue, handler, stop=stop, worker_id="w1", max_attempts=3, wait=wait),  # type: ignore[arg-type]
        timeout=2,
    )
    assert handled == [ok, bad]
    assert queue.released == [(bad, "RuntimeError: LLM timeout", 3)]


async def test_worker_survives_claim_errors() -> None:
    stop = asyncio.Event()
    calls = 0

    class Broken(FakeQueue):
        async def claim_next(self, worker_id: str) -> ClaimedJob | None:
            nonlocal calls
            calls += 1
            raise ConnectionError("db down")

    async def wait(timeout: float) -> bool:
        if calls >= 2:
            stop.set()
        return False

    await asyncio.wait_for(
        run_worker(Broken([]), lambda job: asyncio.sleep(0), stop=stop, worker_id="w1", wait=wait),  # type: ignore[arg-type]
        timeout=2,
    )
    assert calls == 2


# --- ownership: a worker only changes jobs it still owns ---------------------------------


def _where(statement: Any) -> str:
    compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return str(compiled).split(" WHERE ", 1)[1]


@pytest.mark.parametrize(
    "statement",
    [
        release_statement(_job(worker_id="w7"), "boom", retry=True),
        release_statement(_job(worker_id="w7"), "boom", retry=False),
        finish_statement(_job(worker_id="w7"), InvestigationStatus.COMPLETED),
    ],
    ids=["retry", "fail", "finish"],
)
def test_worker_updates_require_the_claiming_worker(statement: Any) -> None:
    where = _where(statement)
    assert "agents.investigations.worker_id = 'w7'" in where
    assert "agents.investigations.status = 'running'" in where


def test_finish_cannot_put_a_job_back_in_the_queue() -> None:
    with pytest.raises(ValueError):
        finish_statement(_job(), InvestigationStatus.QUEUED)


class _Session:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.statements: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.statements.append(statement)
        return SimpleNamespace(rowcount=self.rowcount)


class _Maker:
    """Stands in for `async_sessionmaker` (`.begin()` as an async context manager)."""

    def __init__(self, rowcount: int) -> None:
        self.session = _Session(rowcount)

    def begin(self) -> Any:
        maker = self

        class _Begin:
            async def __aenter__(self) -> _Session:
                return maker.session

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Begin()


async def test_release_reports_a_job_it_no_longer_owns() -> None:
    maker = _Maker(rowcount=0)
    queue = JobQueue(maker)  # type: ignore[arg-type]
    assert await queue.release_for_retry(_job(attempts=1), "boom", max_attempts=3) is None
    assert len(maker.session.statements) == 1, "no NOTIFY when nothing was released"
    assert await queue.finish(_job(), InvestigationStatus.COMPLETED) is False


async def test_release_and_finish_when_still_owned() -> None:
    maker = _Maker(rowcount=1)
    queue = JobQueue(maker)  # type: ignore[arg-type]
    assert await queue.release_for_retry(_job(attempts=1), "boom", max_attempts=3) is InvestigationStatus.QUEUED
    assert len(maker.session.statements) == 2, "the retry is announced with NOTIFY"
    assert await queue.release_for_retry(_job(attempts=3), "boom", max_attempts=3) is InvestigationStatus.FAILED
    assert await queue.finish(_job(), InvestigationStatus.AWAITING_REVIEW) is True
