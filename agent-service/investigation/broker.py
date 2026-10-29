"""In-process wake-ups for the live streams.

- Investigation timelines: events are stored first; `publish_investigation` only bumps a
  version so open SSE streams re-read the store at once. Streams also re-read on a timer,
  which covers events written by another process.
- Case queue: `publish_case` fans a `CaseEvent` out to every open `/cases/events` stream
  (live only, not stored, per the contract).
"""

import asyncio
import contextlib
import uuid
from collections import defaultdict
from typing import Any

CASE_QUEUE_SIZE = 200


class Broker:
    def __init__(self) -> None:
        self._versions: dict[uuid.UUID, int] = defaultdict(int)
        self._waiters: dict[uuid.UUID, asyncio.Event] = {}
        self._case_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    # --- investigations ---------------------------------------------------------------

    def version(self, investigation_id: uuid.UUID) -> int:
        return self._versions[investigation_id]

    def publish_investigation(self, investigation_id: uuid.UUID) -> None:
        self._versions[investigation_id] += 1
        waiter = self._waiters.pop(investigation_id, None)
        if waiter is not None:
            waiter.set()

    async def wait_investigation(self, investigation_id: uuid.UUID, since: int, timeout: float) -> bool:
        """Wait until something was published after version `since`, or the timeout."""
        if self._versions[investigation_id] > since:
            return True
        waiter = self._waiters.setdefault(investigation_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(waiter.wait(), timeout)
        return self._versions[investigation_id] > since

    # --- case queue -----------------------------------------------------------------------

    def subscribe_cases(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CASE_QUEUE_SIZE)
        self._case_subscribers.add(queue)
        return queue

    def unsubscribe_cases(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._case_subscribers.discard(queue)

    def publish_case(self, event: dict[str, Any]) -> None:
        for queue in list(self._case_subscribers):
            with contextlib.suppress(asyncio.QueueFull):  # a stuck client misses updates; it reloads /cases
                queue.put_nowait(event)

    @property
    def case_subscribers(self) -> int:
        return len(self._case_subscribers)
