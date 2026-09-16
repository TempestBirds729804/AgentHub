import uuid

from sqlmodel import Session

from app.models import Conversation
from tests.utils.agent import create_random_agent, create_random_agent_version


def create_random_conversation(
    db: Session, owner_id: uuid.UUID | None = None, *, published: bool = True
) -> Conversation:
    agent = create_random_agent(db, owner_id=owner_id)
    if published:
        create_random_agent_version(db, agent)
    conversation = Conversation(owner_id=agent.owner_id, agent_id=agent.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation
