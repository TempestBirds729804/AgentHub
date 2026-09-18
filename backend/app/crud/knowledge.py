import uuid

from sqlmodel import Session

from app.core.config import settings
from app.models import KnowledgeBase, KnowledgeBaseCreate, KnowledgeBaseUpdate


def create_knowledge_base(
    *, session: Session, knowledge_in: KnowledgeBaseCreate, owner_id: uuid.UUID
) -> KnowledgeBase:
    obj = KnowledgeBase.model_validate(
        knowledge_in,
        update={
            "owner_id": owner_id,
            "embedding_model": settings.EMBEDDING_MODEL,
            "embedding_dim": settings.EMBEDDING_DIM,
        },
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def update_knowledge_base(
    *, session: Session, db_knowledge: KnowledgeBase, knowledge_in: KnowledgeBaseUpdate
) -> KnowledgeBase:
    db_knowledge.sqlmodel_update(knowledge_in.model_dump(exclude_unset=True))
    session.add(db_knowledge)
    session.commit()
    session.refresh(db_knowledge)
    return db_knowledge
