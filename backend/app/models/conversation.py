import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.user import User


class MessageRole(str, enum.Enum):  # noqa: UP042
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class ConversationBase(SQLModel):
    title: str | None = Field(default=None, max_length=255)


class ConversationCreate(ConversationBase):
    agent_id: uuid.UUID


class ConversationUpdate(ConversationBase):
    pass


class Conversation(ConversationBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE", index=True)
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        max_length=128,
        unique=True,
        index=True,
    )
    summary: str | None = Field(default=None, max_length=8000)
    summarized_up_to_seq: int | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    owner: "User" = Relationship(back_populates="conversations")  # noqa: UP037
    agent: "Agent" = Relationship(back_populates="conversations")  # noqa: UP037
    messages: list["ConvMessage"] = Relationship(  # noqa: UP037
        back_populates="conversation", cascade_delete=True
    )


class ConversationPublic(ConversationBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    agent_id: uuid.UUID
    thread_id: str
    summary: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int | None = None


class ConversationsPublic(SQLModel):
    data: list[ConversationPublic]
    count: int


class ConvMessage(SQLModel, table=True):
    __tablename__ = "message"
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    conversation_id: uuid.UUID = Field(
        foreign_key="conversation.id", ondelete="CASCADE", index=True
    )
    seq: int
    role: MessageRole = Field(sa_type=String(32))  # type: ignore
    content: str = Field(default="", sa_type=Text)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    tool_call_id: str | None = Field(default=None, max_length=128)
    token_count: int | None = None
    run_id: uuid.UUID | None = Field(default=None, index=True)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    conversation: Conversation = Relationship(back_populates="messages")


class ConvMessagePublic(SQLModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    seq: int
    role: MessageRole
    content: str
    tool_calls: list[dict[str, Any]]
    tool_call_id: str | None = None
    token_count: int | None = None
    run_id: uuid.UUID | None = None
    created_at: datetime


class ConvMessagesPublic(SQLModel):
    data: list[ConvMessagePublic]
    count: int


class StreamMessageRequest(SQLModel):
    message: str = Field(min_length=1, max_length=20000)
