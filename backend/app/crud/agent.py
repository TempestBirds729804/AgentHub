import uuid

from sqlmodel import Session, func, select

from app.agent.snapshot import AgentSnapshot
from app.models import Agent, AgentCreate, AgentUpdate, AgentVersion
from app.models.base import get_datetime_utc


def create_agent(
    *, session: Session, agent_in: AgentCreate, owner_id: uuid.UUID
) -> Agent:
    db_obj = Agent.model_validate(agent_in, update={"owner_id": owner_id})
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def update_agent(*, session: Session, db_agent: Agent, agent_in: AgentUpdate) -> Agent:
    agent_data = agent_in.model_dump(exclude_unset=True)
    db_agent.sqlmodel_update(agent_data, update={"updated_at": get_datetime_utc()})
    session.add(db_agent)
    session.commit()
    session.refresh(db_agent)
    return db_agent


def get_agent(*, session: Session, agent_id: uuid.UUID) -> Agent | None:
    return session.get(Agent, agent_id)


def get_latest_version_number(*, session: Session, agent_id: uuid.UUID) -> int:
    statement = select(func.max(AgentVersion.version_number)).where(
        AgentVersion.agent_id == agent_id
    )
    return session.exec(statement).one() or 0


def publish_agent_version(
    *, session: Session, db_agent: Agent, changelog: str | None = None
) -> AgentVersion:
    """Freeze the current agent configuration as an immutable version."""
    snapshot = AgentSnapshot(
        name=db_agent.name,
        description=db_agent.description,
        system_prompt=db_agent.system_prompt,
        llm_model=db_agent.llm_model,
        llm_settings=db_agent.llm_settings,
        max_iterations=db_agent.max_iterations,
        timeout_seconds=db_agent.timeout_seconds,
    )
    # Concurrent publishes rely on the database unique constraint for detection.
    next_number = get_latest_version_number(session=session, agent_id=db_agent.id) + 1
    db_version = AgentVersion(
        agent_id=db_agent.id,
        version_number=next_number,
        snapshot=snapshot.model_dump(mode="json"),
        changelog=changelog,
    )
    session.add(db_version)
    session.commit()
    session.refresh(db_version)
    return db_version


def get_agent_version(
    *, session: Session, version_id: uuid.UUID
) -> AgentVersion | None:
    return session.get(AgentVersion, version_id)
