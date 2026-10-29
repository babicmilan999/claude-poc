"""Settings of an eval run, from environment variables (the CLI flags override them)."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from evals.report import THRESHOLDS_FILE
from evals.results import Mode
from evals.runner import DEFAULT_DATASET
from evals.scenario import SCENARIOS_DIR


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    eval_scenarios_dir: Path = SCENARIOS_DIR
    eval_thresholds_file: Path = THRESHOLDS_FILE
    # Which modes to run: journey (simulated customer end to end), assistant, copilot.
    eval_modes: Annotated[list[Mode], NoDecode] = Field(default_factory=lambda: list(Mode))
    # Only these scenario ids (comma-separated); empty runs all.
    eval_only: Annotated[list[str], NoDecode] = Field(default_factory=list)
    eval_run_name: str | None = None
    eval_dataset: str = DEFAULT_DATASET
    eval_max_concurrency: int = Field(default=1, ge=1)
    # Score groundedness with the `judge` alias.
    eval_judge: bool = True

    @field_validator("eval_modes", "eval_only", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("eval_run_name", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value
