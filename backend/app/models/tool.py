import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import StringConstraints
from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.agent import AgentVersion
    from app.models.user import User

ToolName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]+$")]


class ToolType(str, enum.Enum):  # noqa: UP042
    FUNCTION = "function"
    HTTP = "http"
    MCP = "mcp"


class ToolBase(SQLModel):
    name: ToolName = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    tool_type: ToolType = Field(sa_type=String(32))  # type: ignore
    parameters_schema: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    config: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    requires_approval: bool = False
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    is_active: bool = True


class ToolCreate(ToolBase):
    pass


class ToolUpdate(SQLModel):
    name: ToolName | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    parameters_schema: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    requires_approval: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    is_active: bool | None = None


class Tool(ToolBase, table=True):
    __table_args__ = (UniqueConstraint("owner_id", "name"),)
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
    owner: "User" = Relationship(back_populates="tools")  # noqa: UP037
    bindings: list["AgentToolBinding"] = Relationship(  # noqa: UP037
        back_populates="tool", cascade_delete=True
    )


class ToolPublic(ToolBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ToolsPublic(SQLModel):
    data: list[ToolPublic]
    count: int


class AgentToolBinding(SQLModel, table=True):
    __tablename__ = "agent_tool_binding"
    __table_args__ = (UniqueConstraint("agent_version_id", "tool_id"),)
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_version_id: uuid.UUID = Field(
        foreign_key="agent_version.id", nullable=False, ondelete="CASCADE", index=True
    )
    tool_id: uuid.UUID = Field(
        foreign_key="tool.id", nullable=False, ondelete="CASCADE", index=True
    )
    config_override: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    agent_version: "AgentVersion" = Relationship(back_populates="tool_bindings")  # noqa: UP037
    tool: Tool = Relationship(back_populates="bindings")


class BuiltinFunctionPublic(SQLModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]


class BuiltinFunctionsPublic(SQLModel):
    data: list[BuiltinFunctionPublic]
    count: int


class ToolTestRequest(SQLModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolTestResult(SQLModel):
    ok: bool
    content: str
    error: str | None = None
    duration_ms: int
