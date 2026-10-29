"""Client for the banking service's simulation module: `POST /simulation/incidents`
(`contracts/banking-api.yaml`).

Kept apart from `core.banking.BankingClient` on purpose: that client is what the agents'
tools use and is read-only; this one changes banking data and is only called by code
(the Simulate buttons and the eval runner), never by an agent.

The incident carries the answer key (`expected_root_cause`, `expected_outcome`). Callers
split it with `Incident.customer_view()` (what the simulated customer may know) and
`Incident.answer_key()` (for storing and scoring only).
"""

import uuid
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from core.enums import ExpectedOutcome, IssueType, RootCause
from simulation.persona import CustomerView, PersonaSpec


class SimulationError(Exception):
    """The banking simulation failed or returned something unexpected."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SimulationUnavailable(SimulationError):
    """404: the banking service runs without the `simulation` profile."""


class SimulationRejected(SimulationError):
    """400: the banking service rejected the request (for example an unknown root cause)."""


class IncidentRequest(BaseModel):
    """All fields optional: without `type` the banking service picks a random type."""

    type: IssueType | None = None
    root_cause: RootCause | None = None
    customer_id: uuid.UUID | None = None
    params: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class AnswerKey(BaseModel):
    """What the incident should lead to. For scorers only; never shown to an agent."""

    model_config = ConfigDict(frozen=True)

    issue_type: IssueType
    root_cause: RootCause
    outcome: ExpectedOutcome


class Incident(BaseModel):
    """The `Incident` schema of the banking contract."""

    model_config = ConfigDict(extra="ignore")

    incident_id: uuid.UUID
    issue_type: IssueType
    customer_id: uuid.UUID
    customer_name: str
    persona_hints: dict[str, Any] = Field(default_factory=dict)
    customer_knowledge: list[str] = Field(default_factory=list)
    complaint: str | None = None
    affected: dict[str, Any] = Field(default_factory=dict)
    expected_root_cause: RootCause
    expected_outcome: ExpectedOutcome
    created_at: datetime

    def customer_view(self) -> CustomerView:
        """What a real customer in this situation would know. No answer key."""
        hints = {k: v for k, v in self.persona_hints.items() if k in PersonaSpec.model_fields}
        knows = list(self.customer_knowledge) or [str(k) for k in hints.get("knows") or []]
        hints["knows"] = knows
        try:
            persona = PersonaSpec.model_validate(hints)
        except ValueError:
            persona = PersonaSpec(knows=knows)
        return CustomerView(
            customer_id=self.customer_id,
            customer_name=self.customer_name,
            persona=persona,
            complaint=self.complaint,
        )

    def answer_key(self) -> AnswerKey:
        return AnswerKey(issue_type=self.issue_type, root_cause=self.expected_root_cause, outcome=self.expected_outcome)


class SimulationBankingClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "SimulationBankingClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def create_incident(self, request: IncidentRequest | None = None) -> Incident:
        body = (request or IncidentRequest()).body()
        try:
            response = await self._http.post("/simulation/incidents", json=body)
        except httpx.HTTPError as exc:
            raise SimulationError(f"banking service unreachable: {exc.__class__.__name__}") from exc
        if response.status_code == 404:
            raise SimulationUnavailable("the banking service has no simulation module (profile off)", 404)
        if response.status_code == 400:
            raise SimulationRejected(f"incident rejected: {_problem_detail(response)}", 400)
        if response.status_code != 201:
            raise SimulationError(
                f"banking service returned {response.status_code} for an incident", response.status_code
            )
        try:
            return Incident.model_validate(response.json())
        except ValueError as exc:
            raise SimulationError("invalid incident from the banking service") from exc


def _problem_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("title") or data)[:200]
    return str(data)[:200]
