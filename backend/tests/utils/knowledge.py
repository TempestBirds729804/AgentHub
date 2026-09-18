import uuid

from sqlmodel import Session

from app.crud.knowledge import create_knowledge_base
from app.models import KnowledgeBase, KnowledgeBaseCreate
from tests.utils.user import create_random_user
from tests.utils.utils import random_lower_string


def create_random_knowledge_base(
    db: Session, owner_id: uuid.UUID | None = None
) -> KnowledgeBase:
    return create_knowledge_base(
        session=db,
        knowledge_in=KnowledgeBaseCreate(name=random_lower_string()),
        owner_id=owner_id or create_random_user(db).id,
    )
