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
    AgentToolBinding,
    AgentUpdate,
    AgentVersion,
    AgentVersionCreate,
    AgentVersionPublic,
    AgentVersionsPublic,
    Message,
    Tool,
)

router = APIRouter(prefix="/agents", tags=["agents"])


def _validate_tool_ids(
    *, session: SessionDep, current_user: CurrentUser, tool_ids: list[uuid.UUID]
) -> None:
    statement = select(Tool).where(col(Tool.id).in_(tool_ids)).with_for_update()
    if not current_user.is_superuser:
        statement = statement.where(Tool.owner_id == current_user.id)
    tools = session.exec(statement).all()
    if len(tools) != len(set(tool_ids)):
        raise HTTPException(
            status_code=400, detail="Some tool ids are invalid or not owned by you"
        )
    if len({tool.name for tool in tools}) != len(tools):
        raise HTTPException(
            status_code=400, detail="Bound tools must have distinct names"
        )


def _version_public(
    *, session: SessionDep, version: AgentVersion
) -> AgentVersionPublic:
    names = session.exec(
        select(Tool.name)
        .join(AgentToolBinding, col(AgentToolBinding.tool_id) == Tool.id)
        .where(AgentToolBinding.agent_version_id == version.id)
        .order_by(Tool.name)
    ).all()
    return AgentVersionPublic.model_validate(
        version, update={"tool_names": list(names)}
    )


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
    _validate_tool_ids(
        session=session, current_user=current_user, tool_ids=agent_in.tool_ids
    )
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
    if "tool_ids" in agent_in.model_fields_set:
        if agent_in.tool_ids is None:
            raise HTTPException(status_code=422, detail="tool_ids cannot be null")
        _validate_tool_ids(
            session=session, current_user=current_user, tool_ids=agent_in.tool_ids
        )
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
        _validate_tool_ids(
            session=session,
            current_user=current_user,
            tool_ids=[uuid.UUID(str(value)) for value in agent.tool_ids],
        )
        version = crud.publish_agent_version(
            session=session, db_agent=agent, changelog=version_in.changelog
        )
        return _version_public(session=session, version=version)
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
    tool_names: dict[uuid.UUID, list[str]] = {}
    for version_id, name in session.exec(
        select(AgentToolBinding.agent_version_id, Tool.name)
        .join(Tool, col(AgentToolBinding.tool_id) == Tool.id)
        .where(col(AgentToolBinding.agent_version_id).in_([v.id for v in versions]))
        .order_by(Tool.name)
    ).all():
        tool_names.setdefault(version_id, []).append(name)
    versions_public = [
        AgentVersionPublic.model_validate(
            version, update={"tool_names": tool_names.get(version.id, [])}
        )
        for version in versions
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
    return _version_public(session=session, version=version)
