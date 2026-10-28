"""Settings from environment variables (documented in the root `.env.example`)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from core.enums import IssueType

AGENT_SERVICE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # --- LLM gateway ---------------------------------------------------------
    llm_profile: str = "ollama"
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: SecretStr = SecretStr("sk-local-dev")
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2

    # --- Agent service -------------------------------------------------------
    database_url: str = "postgresql://app:app@localhost:5432/app"
    banking_api_url: str = "http://localhost:8080"
    banking_timeout_seconds: float = 10.0
    auto_investigate_types: Annotated[list[IssueType], NoDecode] = Field(
        default_factory=lambda: [
            IssueType.CARD_DECLINE,
            IssueType.MISSED_PAYMENT,
            IssueType.FAILED_DIRECT_DEBIT,
        ]
    )
    max_parallel_investigations: int = Field(default=1, ge=1)
    simulation_enabled: bool = False
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:4200", "http://localhost:4300"]
    )
    triage_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # --- Knowledge base ------------------------------------------------------
    kb_path: Path = AGENT_SERVICE_DIR.parent / "knowledge-base"
    kb_ingest_on_startup: bool = True
    kb_article_base_url: str = "/help/"
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # --- Langfuse ------------------------------------------------------------
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None

    @field_validator("auto_investigate_types", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("langfuse_public_key", "langfuse_secret_key", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def sqlalchemy_url(self) -> str:
        """`DATABASE_URL` with the psycopg (v3) driver, for SQLAlchemy."""
        url = self.database_url
        for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
