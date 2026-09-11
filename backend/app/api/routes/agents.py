import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, func, select

from app import crud
from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Agent,
    AgentCreate,
    AgentPublic,
    AgentsPublic,
    AgentUpdate,
    AgentVersion,
    AgentVersionCreate,
    AgentVersionPublic,
    AgentVersionsPublic,
    Message,
)

router = APIRouter(prefix="/agents", tags=["agents"])


def _get_owned_agent(
    *, session: SessionDep, current_user: CurrentUser, agent_id: uuid.UUID
) -> Agent:
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return agent


@router.get("/", response_model=AgentsPublic)
def read_agents(
    session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100
) -> Any:
    """Retrieve agents."""
    if current_user.is_superuser:
        count_statement = select(func.count()).select_from(Agent)
        statement = select(Agent)
    else:
        count_statement = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.owner_id == current_user.id)
        )
        statement = select(Agent).where(Agent.owner_id == current_user.id)

    count = session.exec(count_statement).one()
    agents = session.exec(
        statement.order_by(col(Agent.created_at).desc()).offset(skip).limit(limit)
    ).all()
    version_counts = dict(
        session.exec(
            select(AgentVersion.agent_id, func.max(AgentVersion.version_number))
            .where(col(AgentVersion.agent_id).in_([agent.id for agent in agents]))
            .group_by(col(AgentVersion.agent_id))
        ).all()
    )
    agents_public = [
        AgentPublic.model_validate(
            agent,
            update={"latest_version_number": version_counts.get(agent.id)},
        )
        for agent in agents
    ]
    return AgentsPublic(data=agents_public, count=count)


@router.get("/{id}", response_model=AgentPublic)
def read_agent(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Get an agent by ID."""
    agent = _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    latest_version_number = crud.get_latest_version_number(
        session=session, agent_id=agent.id
    )
    return AgentPublic.model_validate(
        agent,
        update={"latest_version_number": latest_version_number or None},
    )


@router.post("/", response_model=AgentPublic)
def create_agent(
    *, session: SessionDep, current_user: CurrentUser, agent_in: AgentCreate
) -> Any:
    """Create a new agent."""
    return crud.create_agent(
        session=session, agent_in=agent_in, owner_id=current_user.id
    )


@router.patch("/{id}", response_model=AgentPublic)
def update_agent(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    agent_in: AgentUpdate,
) -> Any:
    """Update an agent."""
    agent = _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    return crud.update_agent(session=session, db_agent=agent, agent_in=agent_in)


@router.delete("/{id}", response_model=Message)
def delete_agent(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """Delete an agent."""
    agent = _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    session.delete(agent)
    session.commit()
    return Message(message="Agent deleted successfully")


@router.post("/{id}/versions", response_model=AgentVersionPublic)
def publish_version(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    version_in: AgentVersionCreate,
) -> Any:
    """Publish a new immutable version of the agent."""
    agent = _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    if not agent.llm_model:
        raise HTTPException(
            status_code=400,
            detail="Agent must have an LLM model configured to publish",
        )
    try:
        return crud.publish_agent_version(
            session=session, db_agent=agent, changelog=version_in.changelog
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Concurrent publish detected, please retry"
        )


@router.get("/{id}/versions", response_model=AgentVersionsPublic)
def read_versions(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Retrieve published versions of an agent."""
    _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    count = session.exec(
        select(func.count())
        .select_from(AgentVersion)
        .where(AgentVersion.agent_id == id)
    ).one()
    versions = session.exec(
        select(AgentVersion)
        .where(AgentVersion.agent_id == id)
        .order_by(col(AgentVersion.version_number).desc())
    ).all()
    versions_public = [
        AgentVersionPublic.model_validate(version) for version in versions
    ]
    return AgentVersionsPublic(data=versions_public, count=count)


@router.get("/{id}/versions/{version_id}", response_model=AgentVersionPublic)
def read_version(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    version_id: uuid.UUID,
) -> Any:
    """Get a published agent version by ID."""
    _get_owned_agent(session=session, current_user=current_user, agent_id=id)
    version = crud.get_agent_version(session=session, version_id=version_id)
    if not version or version.agent_id != id:
        raise HTTPException(status_code=404, detail="Agent version not found")
    return version
