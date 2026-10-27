import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from app.models.base import get_datetime_utc


class AssertionType(str, enum.Enum):  # noqa: UP042
    EXACT = "exact"  # 输出完全等于期望值
    CONTAINS = "contains"  # 输出包含期望值（不区分大小写）
    NOT_CONTAINS = "not_contains"  # 输出不包含某个值
    REGEX = "regex"  # 输出匹配正则
    JSON_PATH = "json_path"  # 输出解析为 JSON 后某个路径的值等于期望
    TOOL_CALLED = "tool_called"  # 执行过程中调用了指定工具
    TOOL_NOT_CALLED = "tool_not_called"
    LLM_JUDGE = "llm_judge"  # 用模型打分，score >= threshold 视为通过
    MAX_DURATION_MS = "max_duration_ms"
    MAX_COST_USD = "max_cost_usd"


class EvalRunStatus(str, enum.Enum):  # noqa: UP042
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvalCaseBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    input: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    expected_output: str | None = Field(default=None, max_length=20000)
    assertions: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    tags: list[str] = Field(default_factory=list, sa_type=JSONB)
    is_active: bool = True


class EvalCaseCreate(EvalCaseBase):
    pass


class EvalCaseUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    input: dict[str, Any] | None = None
    expected_output: str | None = None
    assertions: list[dict[str, Any]] | None = None
    tags: list[str] | None = None
    is_active: bool | None = None


class EvalCase(EvalCaseBase, table=True):
    __tablename__ = "eval_case"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE", index=True
    )
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class EvalCasePublic(EvalCaseBase):
    id: uuid.UUID
    agent_id: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalCasesPublic(SQLModel):
    data: list[EvalCasePublic]
    count: int


class EvalRunCreate(SQLModel):
    agent_version_id: uuid.UUID
    case_ids: list[uuid.UUID] | None = None  # None 表示跑该 agent 的全部启用用例
    tags: list[str] | None = None  # 按标签筛选用例
    note: str | None = Field(default=None, max_length=500)


class EvalRun(SQLModel, table=True):
    __tablename__ = "eval_run"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE", index=True
    )
    agent_version_id: uuid.UUID = Field(
        foreign_key="agent_version.id", nullable=False, ondelete="CASCADE", index=True
    )
    note: str | None = Field(default=None, max_length=500)

    status: EvalRunStatus = Field(
        default=EvalRunStatus.QUEUED,
        sa_type=String(32),  # type: ignore
        index=True,
    )
    error: str | None = Field(default=None, max_length=2000)

    case_snapshots: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    case_count: int = 0
    completed_count: int = 0
    passed_count: int = 0

    avg_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    total_cost_usd: Decimal | None = Field(
        default=None,
        sa_type=Numeric(12, 6),  # type: ignore
    )
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    finished_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    results: list["EvalResult"] = Relationship(  # noqa: UP037
        back_populates="eval_run", cascade_delete=True
    )


class EvalRunPublic(SQLModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    agent_version_id: uuid.UUID
    note: str | None = None
    status: EvalRunStatus
    error: str | None = None
    case_count: int
    completed_count: int
    passed_count: int
    avg_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    total_cost_usd: Decimal | None = None
    total_prompt_tokens: int
    total_completion_tokens: int
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 非表字段
    agent_name: str | None = None
    agent_version_number: int | None = None
    pass_rate: float | None = None


class EvalRunsPublic(SQLModel):
    data: list[EvalRunPublic]
    count: int


class EvalResult(SQLModel, table=True):
    __tablename__ = "eval_result"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    eval_run_id: uuid.UUID = Field(
        foreign_key="eval_run.id", nullable=False, ondelete="CASCADE", index=True
    )
    eval_case_id: uuid.UUID = Field(
        foreign_key="eval_case.id", nullable=False, ondelete="CASCADE", index=True
    )
    # 指向实际执行的 Run，可以点进去看完整轨迹
    run_id: uuid.UUID | None = Field(
        default=None, foreign_key="run.id", ondelete="SET NULL"
    )

    passed: bool = False
    score: float | None = None
    actual_output: str | None = Field(default=None, max_length=20000)
    # 每条断言的结果明细
    assertion_results: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    error: str | None = Field(default=None, max_length=2000)

    duration_ms: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal | None = Field(
        default=None,
        sa_type=Numeric(12, 6),  # type: ignore
    )

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    eval_run: EvalRun | None = Relationship(back_populates="results")


class EvalResultPublic(SQLModel):
    id: uuid.UUID
    eval_run_id: uuid.UUID
    eval_case_id: uuid.UUID
    run_id: uuid.UUID | None = None
    passed: bool
    score: float | None = None
    actual_output: str | None = None
    assertion_results: list[dict[str, Any]]
    error: str | None = None
    duration_ms: int | None = None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal | None = None
    # 非表字段，展示用
    case_name: str | None = None


class EvalResultsPublic(SQLModel):
    data: list[EvalResultPublic]
    count: int


class EvalComparisonItem(SQLModel):
    case_id: uuid.UUID
    case_name: str
    left: EvalResultPublic | None = None
    right: EvalResultPublic | None = None
    verdict: str


class EvalComparisonPublic(SQLModel):
    left: EvalRunPublic
    right: EvalRunPublic
    items: list[EvalComparisonItem]
    regression_count: int
    improvement_count: int
