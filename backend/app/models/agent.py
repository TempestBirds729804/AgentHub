import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.run import Run
    from app.models.user import User


class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str = Field(default="", max_length=20000)
    llm_model: str = Field(default="", max_length=128)
    llm_settings: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    max_iterations: int = Field(default=10, ge=1, le=50)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    is_active: bool = True


class AgentCreate(AgentBase):
    pass


class AgentUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    llm_model: str | None = Field(default=None, max_length=128)
    llm_settings: dict[str, Any] | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    is_active: bool | None = None


class Agent(AgentBase, table=True):
    conversations: list["Conversation"] = Relationship(  # noqa: UP037
        back_populates="agent", cascade_delete=True
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    owner: "User" = Relationship(back_populates="agents")  # noqa: UP037
    versions: list["AgentVersion"] = Relationship(  # noqa: UP037
        back_populates="agent", cascade_delete=True
    )


class AgentPublic(AgentBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None
    latest_version_number: int | None = None


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


class AgentVersionBase(SQLModel):
    changelog: str | None = Field(default=None, max_length=2000)


class AgentVersionCreate(AgentVersionBase):
    """Request body for publishing an immutable agent version."""


class AgentVersion(AgentVersionBase, table=True):
    __tablename__ = "agent_version"
    __table_args__ = (UniqueConstraint("agent_id", "version_number"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True
    )
    version_number: int = Field(nullable=False)
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    is_draft: bool = False
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    agent: Agent | None = Relationship(back_populates="versions")
    runs: list["Run"] = Relationship(  # noqa: UP037
        back_populates="agent_version", cascade_delete=True
    )


class AgentVersionPublic(AgentVersionBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    snapshot: dict[str, Any]
    is_draft: bool
    created_at: datetime | None = None


class AgentVersionsPublic(SQLModel):
    data: list[AgentVersionPublic]
    count: int
