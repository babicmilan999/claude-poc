"""Simulation endpoints (`contracts/agent-api.yaml`, tag `ops-simulation`):
`POST /simulation/customers` and `POST /simulation/emails`.

`core.app` includes `router` and enters `lifespan`. Both endpoints exist only with
`SIMULATION_ENABLED=true`: otherwise the lifespan starts nothing and they answer 404
(problem+json), as the contract says.

Tests can put an httpx transport for the banking simulation in
`ctx.extras["simulation_banking_transport"]`.
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from assistant.service import AssistantService
from core.context import AppContext, Context
from core.enums import IssueType
from investigation.schemas import Case
from investigation.service import InvestigationService
from simulation.banking import SimulationBankingClient, SimulationError
from simulation.cases import CaseStoreUnavailable
from simulation.service import ServiceUnavailable, SimulationService
from simulation.settings import SimulationSettings

log = logging.getLogger(__name__)

SERVICE_KEY = "simulation"
TRANSPORT_KEY = "simulation_banking_transport"
ASSISTANT_KEY = "assistant"
INVESTIGATION_KEY = "investigation"

router = APIRouter()


class SimulationRequest(BaseModel):
    """Optional scenario-type picker. Without `type` a random type is used."""

    model_config = ConfigDict(extra="ignore")

    type: IssueType | None = None


class SimulatedCustomerOut(BaseModel):
    """Deliberately carries no root cause or expected outcome."""

    simulation_id: str
    conversation_id: str
    customer_id: str
    customer_name: str | None = Field(default=None, exclude_if=lambda v: v is None)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI, ctx: AppContext) -> AsyncIterator[None]:
    if not ctx.settings.simulation_enabled:
        yield
        return
    settings = SimulationSettings()
    banking = SimulationBankingClient(
        ctx.settings.banking_api_url,
        timeout=settings.simulation_banking_timeout_seconds,
        transport=ctx.extras.get(TRANSPORT_KEY),
    )

    def assistant() -> AssistantService | None:
        service = ctx.extras.get(ASSISTANT_KEY)
        return service if isinstance(service, AssistantService) else None

    def investigation() -> InvestigationService | None:
        service = ctx.extras.get(INVESTIGATION_KEY)
        return service if isinstance(service, InvestigationService) else None

    service = SimulationService(
        llm=ctx.llm,
        banking=banking,
        settings=settings,
        assistant=assistant,
        investigation=investigation,
        sessionmaker=ctx.sessionmaker,
        jobs=ctx.jobs,
        score_client=ctx.tracing.client,
    )
    ctx.extras[SERVICE_KEY] = service
    log.info("simulation enabled: Simulate customer and Simulate email are available")
    try:
        yield
    finally:
        ctx.extras.pop(SERVICE_KEY, None)
        await service.aclose()
        await banking.aclose()


def get_service(ctx: Context) -> SimulationService:
    if not ctx.settings.simulation_enabled:
        raise HTTPException(status_code=404, detail="simulation is disabled (SIMULATION_ENABLED=false)")
    service = ctx.extras.get(SERVICE_KEY)
    if not isinstance(service, SimulationService):
        raise HTTPException(status_code=503, detail="simulation is starting")
    return service


Service = Annotated[SimulationService, Depends(get_service)]
Request = Annotated[SimulationRequest | None, Body()]


def _issue_type(body: SimulationRequest | None) -> IssueType | None:
    issue_type = body.type if body is not None else None
    if issue_type is IssueType.OTHER:
        raise HTTPException(status_code=400, detail="type 'other' cannot be simulated")
    return issue_type


@router.post(
    "/simulation/customers",
    status_code=202,
    response_model=SimulatedCustomerOut,
    tags=["ops-simulation"],
    operation_id="simulateCustomer",
)
async def simulate_customer(service: Service, body: Request = None) -> SimulatedCustomerOut:
    issue_type = _issue_type(body)
    try:
        started = await service.simulate_customer(issue_type)
    except SimulationError as exc:
        raise HTTPException(status_code=502, detail=f"banking simulation failed: {exc}") from exc
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return SimulatedCustomerOut(
        simulation_id=str(started.simulation_id),
        conversation_id=str(started.conversation_id),
        customer_id=str(started.customer_id),
        customer_name=started.customer_name,
    )


@router.post(
    "/simulation/emails",
    status_code=201,
    response_model=Case,
    tags=["ops-simulation"],
    operation_id="simulateEmail",
)
async def simulate_email(service: Service, body: Request = None) -> Case:
    issue_type = _issue_type(body)
    try:
        return await service.simulate_email(issue_type)
    except SimulationError as exc:
        raise HTTPException(status_code=502, detail=f"banking simulation failed: {exc}") from exc
    except (ServiceUnavailable, CaseStoreUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
