"""Settings of the investigation package, from environment variables.

They live here rather than in `core/settings.py` because only this package reads them.
Shared settings (`AUTO_INVESTIGATE_TYPES`, `MAX_PARALLEL_INVESTIGATIONS`,
`TRIAGE_CONFIDENCE_THRESHOLD`, `DATABASE_URL`) stay in core.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InvestigationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # Start the worker pool in the app lifespan. Tests turn it off and drive jobs by hand.
    investigation_workers_enabled: bool = True
    # Investigator LLM turns per run (between two reviews). Spent → escalate.
    investigation_max_steps: int = Field(default=12, ge=1)
    # Investigator LLM turns per pass of a specific investigator / of the generic one.
    investigator_step_budget: int = Field(default=6, ge=1)
    generic_investigator_step_budget: int = Field(default=4, ge=1)
    # Investigator passes per run (evidence check "not enough, budget left" loops back).
    investigation_max_passes: int = Field(default=3, ge=1)
    # Worker attempts per job before it is failed (and the case goes to needs_manual).
    investigation_max_attempts: int = Field(default=3, ge=1)
    investigation_poll_seconds: float = Field(default=5.0, gt=0)
    investigation_heartbeat_seconds: float = Field(default=30.0, gt=0)
    # A running job without a heartbeat for this long is requeued (its worker died).
    investigation_stale_after_seconds: float = Field(default=300.0, gt=0)
    # How often an open SSE timeline re-reads stored events when nothing woke it up
    # (covers events written by another process).
    investigation_stream_poll_seconds: float = Field(default=2.0, gt=0)
