"""Settings of the simulation package, from environment variables.

`SIMULATION_ENABLED` itself stays in `core.settings` (the app reads it to guard the
endpoints); these are only read here.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SimulationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # Upper bound on the simulated customer's messages, whatever the persona says.
    simulator_max_turns: int = Field(default=6, ge=1, le=20)
    # Timeout of `POST /simulation/incidents` (setting up an incident runs domain rules).
    simulation_banking_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long a simulated case is watched for its diagnosis, to score it in Langfuse.
    simulation_score_timeout_seconds: float = Field(default=600.0, gt=0)
    simulation_score_poll_seconds: float = Field(default=2.0, gt=0)
