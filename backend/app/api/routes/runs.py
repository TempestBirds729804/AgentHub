import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from langchain_core.messages import HumanMessage
from pydantic import ValidationError
from sqlmodel import col, func, select

from app.agent.exceptions import AgentError, ModelNotConfiguredError
from app.agent.models import get_provider
from app.agent.snapshot import AgentSnapshot
from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.models import (
    Agent,
    AgentVersion,
    Conversation,
    Run,
    RunCreate,
    RunEvent,
    RunEventPublic,
    RunEventsPublic,
    RunPublic,
    RunsPublic,
    RunStatus,
)
from app.services.run_service import execute_run

router = APIRouter(prefix="/runs", tags=["runs"])


async def _get_owned_version(
    *, session: AsyncSessionDep, current_user: CurrentUser, version_id: uuid.UUID
) -> tuple[AgentVersion, Agent]:
    result = await session.execute(
        select(AgentVersion, Agent).join(Agent).where(AgentVersion.id == version_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent version not found")
    version, agent = row
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return version, agent


def _get_owned_run(
    *, session: SessionDep, current_user: CurrentUser, run_id: uuid.UUID
) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not current_user.is_superuser and run.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return run


@router.post("/", response_model=RunPublic)
async def create_run(
    *, session: AsyncSessionDep, current_user: CurrentUser, run_in: RunCreate
) -> Any:
    """Create and execute an agent run, blocking until it finishes."""
    version, agent = await _get_owned_version(
        session=session, current_user=current_user, version_id=run_in.agent_version_id
    )
    if run_in.conversation_id is not None:
        conversation = await session.get(Conversation, run_in.conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if not current_user.is_superuser and conversation.owner_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not enough permissions")
        if conversation.agent_id != agent.id:
            raise HTTPException(
                status_code=400, detail="Conversation belongs to another agent"
            )
    try:
        snapshot = AgentSnapshot.model_validate(version.snapshot)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid agent version snapshot")
    message = run_in.input.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(
            status_code=422, detail="input.message must be a non-empty string"
        )
    try:
        get_provider()
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Capture response metadata before execution: rollback expires ORM objects.
    metadata = {
        "agent_name": agent.name,
        "agent_version_number": version.version_number,
    }
    run = Run.model_validate(run_in, update={"owner_id": current_user.id})
    session.add(run)
    await session.commit()
    await session.refresh(run)
    try:
        await execute_run(
            session=session,
            run=run,
            snapshot=snapshot,
            initial_messages=[HumanMessage(content=message)],
        )
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except AgentError:
        await session.refresh(run)
    return RunPublic.model_validate(run, update=metadata)


@router.get("/", response_model=RunsPublic)
def read_runs(
    session: SessionDep,
    current_user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    agent_version_id: uuid.UUID | None = None,
    status: RunStatus | None = None,
) -> Any:
    """Retrieve runs filtered by agent version and status."""
    filters = []
    if not current_user.is_superuser:
        filters.append(Run.owner_id == current_user.id)
    if agent_version_id is not None:
        filters.append(Run.agent_version_id == agent_version_id)
    if status is not None:
        filters.append(Run.status == status)
    count = session.exec(select(func.count()).select_from(Run).where(*filters)).one()
    rows = session.exec(
        select(Run, Agent.name, AgentVersion.version_number)
        .join(AgentVersion, col(Run.agent_version_id) == col(AgentVersion.id))
        .join(Agent, col(AgentVersion.agent_id) == col(Agent.id))
        .where(*filters)
        .order_by(col(Run.created_at).desc(), col(Run.id).desc())
        .offset(skip)
        .limit(limit)
    ).all()
    return RunsPublic(
        data=[
            RunPublic.model_validate(
                run, update={"agent_name": name, "agent_version_number": version_number}
            )
            for run, name, version_number in rows
        ],
        count=count,
    )


@router.get("/{id}", response_model=RunPublic)
def read_run(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Get a run by ID."""
    run = _get_owned_run(session=session, current_user=current_user, run_id=id)
    name, version_number = session.exec(
        select(Agent.name, AgentVersion.version_number)
        .join(AgentVersion, col(Agent.id) == col(AgentVersion.agent_id))
        .where(AgentVersion.id == run.agent_version_id)
    ).one()
    return RunPublic.model_validate(
        run, update={"agent_name": name, "agent_version_number": version_number}
    )


@router.get("/{id}/events", response_model=RunEventsPublic)
def read_events(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Any:
    """Retrieve run events ordered by sequence number."""
    _get_owned_run(session=session, current_user=current_user, run_id=id)
    count = session.exec(
        select(func.count()).select_from(RunEvent).where(RunEvent.run_id == id)
    ).one()
    events = session.exec(
        select(RunEvent)
        .where(RunEvent.run_id == id)
        .order_by(col(RunEvent.seq))
        .offset(skip)
        .limit(limit)
    ).all()
    return RunEventsPublic(
        data=[RunEventPublic.model_validate(event) for event in events], count=count
    )
