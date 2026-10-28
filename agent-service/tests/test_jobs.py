"""Worker loop logic, without Postgres (the queue itself is tested in
`test_integration_postgres.py`)."""

import asyncio
import uuid

from core.enums import InvestigationStatus
from core.jobs import ClaimedJob, run_worker


class FakeQueue:
    def __init__(self, jobs: list[ClaimedJob]) -> None:
        self.jobs = list(jobs)
        self.released: list[tuple[ClaimedJob, str, int]] = []
        self.claims = 0

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        self.claims += 1
        return self.jobs.pop(0) if self.jobs else None

    async def release_for_retry(self, job: ClaimedJob, error: str, *, max_attempts: int) -> InvestigationStatus:
        self.released.append((job, error, max_attempts))
        return InvestigationStatus.QUEUED


def _job(attempts: int = 1) -> ClaimedJob:
    return ClaimedJob(
        investigation_id=uuid.uuid4(), case_id=uuid.uuid4(), trigger="auto", attempts=attempts, resume_payload=None
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
