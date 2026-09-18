import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.agent import AgentVersion
    from app.models.approval import ApprovalRequest
    from app.models.user import User


class RunStatus(str, enum.Enum):  # noqa: UP042
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(str, enum.Enum):  # noqa: UP042
    PLAYGROUND = "playground"
    API = "api"
    EVAL = "eval"


class RunEventType(str, enum.Enum):  # noqa: UP042
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    MODEL_CHUNK = "model_chunk"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    CONTEXT_RETRIEVED = "context_retrieved"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


class RunCreate(SQLModel):
    agent_version_id: uuid.UUID
    input: dict[str, Any] = Field(default_factory=dict)
    trigger: RunTrigger = RunTrigger.PLAYGROUND
    conversation_id: uuid.UUID | None = None


class Run(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    agent_version_id: uuid.UUID = Field(
        foreign_key="agent_version.id", nullable=False, ondelete="CASCADE", index=True
    )
    conversation_id: uuid.UUID | None = Field(
        default=None, foreign_key="conversation.id", ondelete="SET NULL", index=True
    )
    # SQLModel otherwise infers a native SQLAlchemy Enum even for str enums.
    status: RunStatus = Field(default=RunStatus.QUEUED, sa_type=String(32), index=True)  # type: ignore
    trigger: RunTrigger = Field(default=RunTrigger.PLAYGROUND, sa_type=String(32))  # type: ignore
    input: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    output: dict[str, Any] | None = Field(default=None, sa_type=JSONB)
    error: str | None = Field(default=None, max_length=4000)
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore
    duration_ms: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal | None = Field(default=None, sa_type=Numeric(12, 6))  # type: ignore
    thread_id: str | None = Field(default=None, max_length=128, index=True)
    checkpoint_id: str | None = Field(default=None, max_length=128)
    retry_count: int = Field(default=0, ge=0, sa_column_kwargs={"server_default": "0"})
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    owner: "User" = Relationship(back_populates="runs")  # noqa: UP037
    agent_version: "AgentVersion" = Relationship(back_populates="runs")  # noqa: UP037
    events: list["RunEvent"] = Relationship(  # noqa: UP037
        back_populates="run", cascade_delete=True
    )
    approvals: list["ApprovalRequest"] = Relationship(  # noqa: UP037
        back_populates="run", cascade_delete=True
    )


class RunPublic(SQLModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    agent_version_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    status: RunStatus
    trigger: RunTrigger
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal | None = None
    created_at: datetime
    agent_name: str | None = None
    agent_version_number: int | None = None
    thread_id: str | None = None
    checkpoint_id: str | None = None
    retry_count: int = 0


class RunsPublic(SQLModel):
    data: list[RunPublic]
    count: int


class RunEvent(SQLModel, table=True):
    __tablename__ = "run_event"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(
        foreign_key="run.id", nullable=False, ondelete="CASCADE", index=True
    )
    seq: int = Field(nullable=False)
    event_type: RunEventType = Field(nullable=False, sa_type=String(32))  # type: ignore
    node_name: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    run: Run = Relationship(back_populates="events")


class RunEventPublic(SQLModel):
    id: uuid.UUID
    run_id: uuid.UUID
    seq: int
    event_type: RunEventType
    node_name: str | None = None
    payload: dict[str, Any]
    created_at: datetime


class RunEventsPublic(SQLModel):
    data: list[RunEventPublic]
    count: int
