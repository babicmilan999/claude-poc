"""The investigation worker pool (architecture section 5), started by the app lifespan.

`MAX_PARALLEL_INVESTIGATIONS` workers run `core.jobs.run_worker` with
`InvestigationService.handle_job`. With Postgres they sleep until `NOTIFY
investigation_jobs` (or the poll interval); a maintenance task requeues the jobs of dead
workers. `CaseEventsListener` forwards `NOTIFY case_events` from other processes to this
process's `/cases/events` streams.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable

from core.jobs import INVESTIGATION_CHANNEL, NotificationListener, run_worker
from investigation.service import ORIGIN, InvestigationService, case_event_from_notification

log = logging.getLogger(__name__)

STOP_GRACE_SECONDS = 5.0


class WorkerPool:
    def __init__(self, service: InvestigationService, *, count: int, database_url: str | None = None) -> None:
        self.service = service
        self.count = count
        self.database_url = database_url
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._listener: NotificationListener | None = None

    async def start(self) -> None:
        settings = self.service.settings
        wait: Callable[[float], Awaitable[bool]] | None = None
        if self.database_url:
            listener = NotificationListener(self.database_url, [INVESTIGATION_CHANNEL])
            try:
                await listener.start()
                self._listener = listener
                wait = listener.wait
            except Exception:
                log.warning("LISTEN %s failed; workers poll only", INVESTIGATION_CHANNEL, exc_info=True)
        prefix = uuid.uuid4().hex[:6]
        for i in range(self.count):
            self._tasks.append(
                asyncio.create_task(
                    run_worker(
                        self.service.store,  # type: ignore[arg-type]  # claim_next / release_for_retry
                        self.service.handle_job,
                        stop=self._stop,
                        worker_id=f"{ORIGIN}-{prefix}-{i}",
                        max_attempts=settings.investigation_max_attempts,
                        poll_interval=settings.investigation_poll_seconds,
                        wait=wait,
                    ),
                    name=f"investigation-worker-{i}",
                )
            )
        self._tasks.append(asyncio.create_task(self._maintenance(), name="investigation-maintenance"))
        log.info("started %d investigation worker(s)", self.count)

    async def _maintenance(self) -> None:
        interval = max(5.0, self.service.settings.investigation_stale_after_seconds / 4)
        while not self._stop.is_set():
            try:
                await self.service.recover_stale()
            except Exception:
                log.warning("stale job recovery failed", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=STOP_GRACE_SECONDS)
            for task in pending:  # a job still running is requeued later as stale
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._listener is not None:
            await self._listener.close()


class CaseEventsListener:
    """`LISTEN case_events`; forwards case queue events published by other processes."""

    def __init__(self, service: InvestigationService, database_url: str, channel: str) -> None:
        self.service = service
        self.database_url = database_url
        self.channel = channel
        self._conn: object | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self.database_url, autocommit=True)
        await conn.execute(f'LISTEN "{self.channel}"')
        self._conn = conn
        self._task = asyncio.create_task(self._read(conn), name="case-events-listener")

    async def _read(self, conn: object) -> None:
        try:
            async for notify in conn.notifies():  # type: ignore[attr-defined]
                origin, event, case_id = case_event_from_notification(notify.payload)
                if origin == ORIGIN:
                    continue
                if event is not None:
                    self.service.publish_case_event(event, notify=False)
                elif case_id is not None:
                    try:
                        case = await self.service.store.get_case(uuid.UUID(case_id))
                    except ValueError:
                        continue
                    if case is not None:
                        self.service.publish_case_created(case, notify=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("case events listener stopped; /cases/events only shows this process's updates")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._conn is not None:
            await self._conn.close()  # type: ignore[attr-defined]
