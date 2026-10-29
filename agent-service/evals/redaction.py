"""Answer-key redaction for eval traces.

`core.masking` redacts the `expected_root_cause` / `expected_outcome` fields by name. A
scenario carries its answer under other names too: `incident.root_cause` (what the
banking simulation sets up) and `expected.{root_cause, outcome}`. `root_cause` cannot be
redacted everywhere (it is the copilot's own diagnosis), so it is redacted only inside
the structures that hold an answer key.

Eval runs build their Langfuse client with `eval_langfuse_mask`: the core mask plus this
redaction, so if a scenario ever reaches a trace input, output or metadata value, its
answer does not.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from core.masking import REDACTED, langfuse_mask
from core.settings import Settings

# Keys whose value is (or contains) a scenario's answer key.
ANSWER_CONTAINERS = frozenset({"incident", "expected", "expected_output", "answer_key"})
# Inside those, the fields that state the answer.
ANSWER_FIELDS = frozenset({"root_cause", "outcome", "expected_root_cause", "expected_outcome"})


def redact_answer_key(data: Any, *, inside: bool = False) -> Any:
    """A copy of a JSON-like structure with the answer fields of any incident / expected
    block replaced by `[REDACTED]`."""
    if isinstance(data, BaseModel):
        return redact_answer_key(data.model_dump(mode="json"), inside=inside)
    if isinstance(data, Mapping):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            name = str(key).lower()
            if inside and name in ANSWER_FIELDS:
                out[key] = REDACTED
            else:
                out[key] = redact_answer_key(value, inside=inside or name in ANSWER_CONTAINERS)
        return out
    if isinstance(data, list | tuple):
        return [redact_answer_key(v, inside=inside) for v in data]
    return data


def eval_langfuse_mask(*, data: Any, **kwargs: Any) -> Any:
    """`Langfuse(mask=...)` for eval runs: answer-key redaction, then the core mask."""
    try:
        return langfuse_mask(data=redact_answer_key(data), **kwargs)
    except Exception:  # never let masking errors leak the original data
        return REDACTED


def init_eval_langfuse(settings: Settings) -> Any | None:
    """Like `core.tracing.init_langfuse`, with `eval_langfuse_mask`."""
    if not settings.langfuse_enabled:
        return None
    from langfuse import Langfuse

    assert settings.langfuse_secret_key is not None
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
        mask=eval_langfuse_mask,
        environment=f"profile-{settings.llm_profile}",
    )
