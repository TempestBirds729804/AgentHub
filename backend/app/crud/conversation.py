import uuid

from sqlmodel import Session

from app.models import (
    Conversation,
    ConversationCreate,
    ConversationUpdate,
    get_datetime_utc,
)


def create_conversation(
    *, session: Session, conversation_in: ConversationCreate, owner_id: uuid.UUID
) -> Conversation:
    conversation = Conversation.model_validate(
        conversation_in, update={"owner_id": owner_id}
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return conversation


def update_conversation(
    *, session: Session, conversation: Conversation, conversation_in: ConversationUpdate
) -> Conversation:
    conversation.sqlmodel_update(
        conversation_in.model_dump(exclude_unset=True),
        update={"updated_at": get_datetime_utc()},
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return conversation
