"""Experiment backends: Langfuse's SDK experiments when tracing is on, a local runner with
the same call shape when it is off (no keys, no network).

`LocalExperiments.run_experiment` mirrors `Langfuse.run_experiment`: it calls
`task(item=...)` for each item and every evaluator with `input`, `output`,
`expected_output` and `metadata`, and returns results with `.item_results`, each with
`.item`, `.output` and `.evaluations`. Like the SDK, it logs and skips what fails: an
evaluator that raises adds no evaluations, an item whose task raises has no result. The
gate (`evals.report.check_gate`) fails on both.
"""

import asyncio
import concurrent.futures
import inspect
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from langfuse.experiment import Evaluation

log = logging.getLogger(__name__)


class ExperimentBackend(Protocol):
    def run_experiment(
        self,
        *,
        name: str,
        run_name: str | None = None,
        description: str | None = None,
        data: Sequence[Any],
        task: Callable[..., Any],
        evaluators: Sequence[Callable[..., Any]] = (),
        max_concurrency: int = 1,
        metadata: dict[str, str] | None = None,
    ) -> Any: ...


def item_field(item: Any, name: str) -> Any:
    """`input` / `expected_output` / `metadata` of a local dict item or a Langfuse DatasetItem."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def normalise_evaluations(result: Any) -> list[Evaluation]:
    if result is None:
        return []
    if isinstance(result, Evaluation):
        return [result]
    if isinstance(result, dict):
        return [Evaluation(**result)]
    return [r if isinstance(r, Evaluation) else Evaluation(**r) for r in result]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion from sync code, also when a loop is already running
    (then in a worker thread), like the Langfuse SDK does."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@dataclass
class LocalItemResult:
    item: Any
    output: Any
    evaluations: list[Evaluation] = field(default_factory=list)
    trace_id: str | None = None


@dataclass
class LocalExperimentResult:
    name: str
    run_name: str
    item_results: list[LocalItemResult]
    metadata: dict[str, str] = field(default_factory=dict)


class LocalExperiments:
    def run_experiment(
        self,
        *,
        name: str,
        run_name: str | None = None,
        description: str | None = None,
        data: Sequence[Any],
        task: Callable[..., Any],
        evaluators: Sequence[Callable[..., Any]] = (),
        max_concurrency: int = 1,
        metadata: dict[str, str] | None = None,
    ) -> LocalExperimentResult:
        return run_sync(
            self._run(
                name=name,
                run_name=run_name or name,
                data=list(data),
                task=task,
                evaluators=list(evaluators),
                max_concurrency=max(1, max_concurrency),
                metadata=dict(metadata or {}),
            )
        )

    async def _run(
        self,
        *,
        name: str,
        run_name: str,
        data: list[Any],
        task: Callable[..., Any],
        evaluators: list[Callable[..., Any]],
        max_concurrency: int,
        metadata: dict[str, str],
    ) -> LocalExperimentResult:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def one(item: Any) -> LocalItemResult | None:
            async with semaphore:
                try:
                    output = await _maybe_await(task(item=item))
                except Exception:
                    log.exception("experiment task failed; the item has no result")
                    return None
                evaluations: list[Evaluation] = []
                for evaluator in evaluators:
                    try:
                        result = await _maybe_await(
                            evaluator(
                                input=item_field(item, "input"),
                                output=output,
                                expected_output=item_field(item, "expected_output"),
                                metadata=item_field(item, "metadata"),
                            )
                        )
                        evaluations += normalise_evaluations(result)
                    except Exception:
                        log.exception("evaluator failed; its scores are missing")
                return LocalItemResult(item=item, output=output, evaluations=evaluations)

        results = await asyncio.gather(*(one(item) for item in data))
        return LocalExperimentResult(
            name=name, run_name=run_name, item_results=[r for r in results if r is not None], metadata=metadata
        )
