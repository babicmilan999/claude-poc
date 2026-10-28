"""Langfuse tracing (architecture section 10).

- The Langfuse client is created with `mask=langfuse_mask`, so every trace input, output
  and metadata value is masked (card numbers, account identifiers, answer keys) before it
  leaves the service.
- Graphs get Langfuse's LangChain callback handler through `Tracing.config(...)`; code
  outside the graphs can use Langfuse's `@observe`.
- Every trace carries the LiteLLM profile and prompt versions as metadata; each LLM call
  also carries its alias (set by `core.llm`).
- Without `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`, tracing is off and
  `config()` returns no callbacks. Only the agent service sends traces.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig

from core.masking import langfuse_mask, mask_data
from core.settings import Settings

log = logging.getLogger(__name__)


def init_langfuse(settings: Settings) -> Any | None:
    if not settings.langfuse_enabled:
        log.info("Langfuse tracing disabled (no keys)")
        return None
    from langfuse import Langfuse

    assert settings.langfuse_secret_key is not None
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
        mask=langfuse_mask,
        environment=f"profile-{settings.llm_profile}",
    )


@dataclass
class Tracing:
    client: Any | None
    profile: str
    default_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Tracing":
        return cls(client=init_langfuse(settings), profile=settings.llm_profile)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def callbacks(self) -> list[Any]:
        if self.client is None:
            return []
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]

    def config(
        self,
        *,
        run_name: str,
        session_id: str | None = None,
        user_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> RunnableConfig:
        """A RunnableConfig for one graph run or LLM call, with Langfuse attributes."""
        meta: dict[str, Any] = {"llm_profile": self.profile, **(metadata or {})}
        all_tags = [*self.default_tags, f"profile:{self.profile}", *(tags or [])]
        if session_id:
            meta["langfuse_session_id"] = session_id
        if user_id:
            meta["langfuse_user_id"] = user_id
        meta["langfuse_tags"] = all_tags
        config: RunnableConfig = {
            "run_name": run_name,
            "callbacks": self.callbacks(),
            "metadata": mask_data(meta),
            "tags": all_tags,
        }
        config.update(extra)  # type: ignore[typeddict-item]
        return config

    def flush(self) -> None:
        if self.client is not None:
            try:
                self.client.flush()
            except Exception:  # tracing must never break the app
                log.warning("Langfuse flush failed", exc_info=True)

    def shutdown(self) -> None:
        if self.client is not None:
            try:
                self.client.shutdown()
            except Exception:
                log.warning("Langfuse shutdown failed", exc_info=True)
