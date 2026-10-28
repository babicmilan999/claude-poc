"""Helpers shared by the graph nodes: emitting timeline events and recording evidence.

Nodes emit events through an `emit` callable in `config["configurable"]`, provided by the
runner for each run (it stores the event and wakes the SSE streams). Without one (for
example a graph run in a notebook) events are dropped.
"""

import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from core.enums import EvidenceOrigin
from core.masking import mask_data

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


async def emit(config: RunnableConfig | None, type_: str, payload: dict[str, Any]) -> None:
    sink = ((config or {}).get("configurable") or {}).get("emit")
    if sink is not None:
        await sink(type_, payload)


async def step_started(config: RunnableConfig | None, node: str, step: int) -> None:
    await emit(config, "step_started", {"node": node, "step": step})


_SUMMARY_KEYS = (
    "status",
    "status_reason",
    "decline_reason",
    "failure_reason",
    "failure_code",
    "result",
    "rule_id",
    "action",
    "account_status",
    "cancelled_by",
)
_EVIDENCE_ID_RE = re.compile(r"^ev-(\d+)$")


def summarise(tool: str, data: Any) -> str:
    """One line for the timeline: record count plus the status and reason codes seen."""
    if isinstance(data, dict) and "error" in data:
        return f"{tool}: {data.get('error')}"
    items = data if isinstance(data, list) else [data]
    facts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in _SUMMARY_KEYS:
            value = item.get(key)
            if value is not None and value != "":
                fact = f"{key}={value}"
                if fact not in facts:
                    facts.append(fact)
    head = f"{tool}: {len(items)} record(s)" if isinstance(data, list) else f"{tool}: 1 record"
    return head + (" (" + ", ".join(facts[:6]) + ")" if facts else "")


def next_evidence_id(taken: Iterable[str]) -> str:
    numbers = [int(m.group(1)) for t in taken if (m := _EVIDENCE_ID_RE.match(t))]
    return f"ev-{max(numbers, default=0) + 1}"


def make_evidence(
    *, evidence_id: str, tool: str, args: dict[str, Any], data: Any, origin: EvidenceOrigin, now: datetime | None = None
) -> dict[str, Any]:
    masked = mask_data(data)
    return {
        "id": evidence_id,
        "origin": origin.value,
        "tool": tool,
        "args": mask_data(dict(args)),
        "summary": summarise(tool, masked),
        "data": masked,
        "created_at": (now or datetime.now(UTC)).isoformat(),
    }


def normalise_handoff_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assistant's evidence as investigation evidence (origin `assistant`), with
    unique ids so the copilot can cite it too."""
    out: list[dict[str, Any]] = []
    taken: list[str] = []
    for item in items:
        evidence_id = str(item.get("id") or "")
        if not evidence_id or evidence_id in taken:
            evidence_id = next_evidence_id(taken)
        taken.append(evidence_id)
        tool = str(item.get("tool") or "assistant")
        data = item.get("data")
        out.append(
            {
                "id": evidence_id,
                "origin": EvidenceOrigin.ASSISTANT.value,
                "tool": tool,
                "args": mask_data(item.get("args") or {}),
                "summary": str(item.get("summary") or summarise(tool, mask_data(data))),
                "data": mask_data(data),
                "created_at": str(item.get("created_at") or datetime.now(UTC).isoformat()),
            }
        )
    return out


def evidence_lines(evidence: list[dict[str, Any]], *, with_data: bool = False, limit_chars: int = 1500) -> str:
    """Evidence as prompt text, one item per line (optionally with a clipped JSON body)."""
    import json

    lines = []
    for e in evidence:
        line = f"- {e['id']} [{e['origin']}] {e['summary']}"
        if with_data and e.get("data") is not None:
            body = json.dumps(e["data"], ensure_ascii=False, default=str)
            line += "\n  data: " + (body if len(body) <= limit_chars else body[:limit_chars] + " …")
        lines.append(line)
    return "\n".join(lines) if lines else "- (no evidence)"
