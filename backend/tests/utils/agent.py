import uuid

from sqlmodel import Session

from app import crud
from app.models import Agent, AgentCreate, AgentVersion
from tests.utils.user import create_random_user
from tests.utils.utils import random_lower_string


def create_random_agent(db: Session, owner_id: uuid.UUID | None = None) -> Agent:
    if owner_id is None:
        owner_id = create_random_user(db).id
    agent_in = AgentCreate(
        name=random_lower_string(),
        description=random_lower_string(),
        system_prompt="You are a helpful assistant.",
        llm_model="gpt-4o-mini",
    )
    return crud.create_agent(session=db, agent_in=agent_in, owner_id=owner_id)


def create_random_agent_version(db: Session, agent: Agent) -> AgentVersion:
    return crud.publish_agent_version(session=db, db_agent=agent, changelog="test")
