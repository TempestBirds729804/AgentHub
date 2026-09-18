import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

from app.models.base import get_datetime_utc

if TYPE_CHECKING:
    from app.models.run import Run


class ApprovalStatus(str, enum.Enum):  # noqa: UP042
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalRequestBase(SQLModel):
    tool_name: str = Field(max_length=64)
    tool_call_id: str = Field(max_length=128)
    tool_args: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    reason: str | None = Field(default=None, max_length=1000)


class ApprovalRequestCreate(ApprovalRequestBase):
    run_id: uuid.UUID
    owner_id: uuid.UUID
    thread_id: str = Field(max_length=128)
    checkpoint_id: str = Field(max_length=128)


class ApprovalDecision(SQLModel):
    approved: bool
    rejection_reason: str | None = Field(default=None, max_length=1000)


class ApprovalRequestUpdate(SQLModel):
    status: ApprovalStatus
    resolved_by_id: uuid.UUID | None = None
    resolved_at: datetime | None = None
    rejection_reason: str | None = Field(default=None, max_length=1000)


class ApprovalRequest(ApprovalRequestBase, table=True):
    __tablename__ = "approval_request"
    __table_args__ = (UniqueConstraint("run_id", "checkpoint_id", "tool_call_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(foreign_key="run.id", ondelete="CASCADE", index=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    status: ApprovalStatus = Field(
        default=ApprovalStatus.PENDING,
        sa_type=String(32),  # type: ignore
        index=True,
    )
    resolved_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore
    rejection_reason: str | None = Field(default=None, max_length=1000)
    checkpoint_id: str = Field(max_length=128)
    thread_id: str = Field(max_length=128)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    run: "Run" = Relationship(back_populates="approvals")  # noqa: UP037


class ApprovalRequestPublic(ApprovalRequestBase):
    id: uuid.UUID
    run_id: uuid.UUID
    owner_id: uuid.UUID
    status: ApprovalStatus
    resolved_by_id: uuid.UUID | None = None
    resolved_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    agent_name: str | None = None


class ApprovalRequestsPublic(SQLModel):
    data: list[ApprovalRequestPublic]
    count: int
