import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, distinct, func, text
from sqlalchemy import select as sa_select
from sqlmodel import SQLModel, col, select

from app.api.deps import SessionDep, get_current_active_superuser
from app.core.async_db import async_engine
from app.core.config import settings
from app.core.redis import get_redis
from app.core.storage import get_minio_client
from app.models import Agent, AgentVersion, ApprovalRequest, Document, Run, User
from app.models.base import get_datetime_utc

router = APIRouter(
    prefix="/monitoring",
    tags=["monitoring"],
    dependencies=[Depends(get_current_active_superuser)],
)


class MonitoringOverview(SQLModel):
    total_users: int
    active_users_7d: int
    total_agents: int
    total_runs: int
    runs_24h: int
    success_rate_24h: float | None
    avg_duration_ms_24h: int | None
    total_cost_usd_30d: Decimal | None
    total_tokens_30d: int
    pending_approvals: int
    documents_processing: int


class UsageDay(SQLModel):
    day: date
    runs: int
    tokens: int
    cost: Decimal | None


class UsagePublic(SQLModel):
    data: list[UsageDay]
    count: int


class UserUsage(SQLModel):
    user_id: uuid.UUID
    email: str
    runs: int
    tokens: int
    cost: Decimal | None


class UsersUsagePublic(SQLModel):
    data: list[UserUsage]
    count: int


class FailurePublic(SQLModel):
    id: uuid.UUID
    agent_name: str
    email: str
    error: str | None
    created_at: datetime


class FailuresPublic(SQLModel):
    data: list[FailurePublic]
    count: int


class ComponentStatus(SQLModel):
    name: str
    healthy: bool
    detail: str
    latency_ms: int


class SystemStatus(SQLModel):
    components: list[ComponentStatus]
    queue_depth: int | None


@router.get("/overview", response_model=MonitoringOverview)
def read_overview(session: SessionDep) -> Any:
    """Retrieve platform totals and recent run statistics."""
    now = get_datetime_utc()
    day, week, month = (now - timedelta(days=n) for n in (1, 7, 30))
    recent = col(Run.created_at) >= day
    monthly = col(Run.created_at) >= month
    row = (
        session.connection()
        .execute(
            sa_select(
                func.count(col(Run.id)),
                func.count(case((recent, 1))),
                func.count(case((recent & (Run.status == "succeeded"), 1))),
                func.avg(case((recent, Run.duration_ms))),
                func.count(distinct(case((col(Run.created_at) >= week, Run.owner_id)))),
                func.sum(
                    case((monthly, Run.prompt_tokens + Run.completion_tokens), else_=0)
                ),
                func.sum(case((monthly, Run.cost_usd), else_=0)),
                func.count(case((monthly & col(Run.cost_usd).is_(None), 1))),
            )
        )
        .one()
    )
    totals = session.exec(
        select(
            select(func.count()).select_from(User).scalar_subquery(),
            select(func.count()).select_from(Agent).scalar_subquery(),
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.status == "pending")
            .scalar_subquery(),
            select(func.count())
            .select_from(Document)
            .where(col(Document.status).in_(["pending", "processing"]))
            .scalar_subquery(),
        )
    ).one()
    return MonitoringOverview(
        total_users=totals[0],
        total_agents=totals[1],
        pending_approvals=totals[2],
        documents_processing=totals[3],
        total_runs=row[0],
        runs_24h=row[1],
        success_rate_24h=row[2] / row[1] if row[1] else None,
        avg_duration_ms_24h=round(row[3]) if row[3] is not None else None,
        active_users_7d=row[4],
        total_tokens_30d=row[5] or 0,
        total_cost_usd_30d=None if row[7] else row[6] or Decimal(0),
    )


@router.get("/usage", response_model=UsagePublic)
def read_usage(session: SessionDep, days: int = Query(30, ge=1, le=90)) -> Any:
    """Retrieve UTC daily usage, including days without runs."""
    start = get_datetime_utc().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days - 1)
    day = func.date_trunc("day", func.timezone("UTC", Run.created_at))
    rows = (
        session.connection()
        .execute(
            sa_select(
                day,
                func.count(col(Run.id)),
                func.sum(Run.prompt_tokens + Run.completion_tokens),
                func.sum(Run.cost_usd),
                func.count(col(Run.cost_usd)),
            )
            .where(col(Run.created_at) >= start)
            .group_by(day)
            .order_by(day)
        )
        .all()
    )
    by_day = {
        r[0].date(): UsageDay(
            day=r[0].date(), runs=r[1], tokens=r[2], cost=r[3] if r[4] == r[1] else None
        )
        for r in rows
    }
    data = [
        by_day.get(d, UsageDay(day=d, runs=0, tokens=0, cost=Decimal(0)))
        for d in (start.date() + timedelta(days=i) for i in range(days))
    ]
    return UsagePublic(data=data, count=len(data))


@router.get("/top-users", response_model=UsersUsagePublic)
def read_top_users(session: SessionDep, limit: int = Query(10, ge=1, le=50)) -> Any:
    """Retrieve the thirty-day user usage ranking."""
    rows = (
        session.connection()
        .execute(
            sa_select(
                col(User.id),
                col(User.email),
                func.count(col(Run.id)),
                func.sum(Run.prompt_tokens + Run.completion_tokens),
                func.sum(Run.cost_usd),
                func.count(col(Run.cost_usd)),
            )
            .join(Run, col(Run.owner_id) == User.id)
            .where(col(Run.created_at) >= get_datetime_utc() - timedelta(days=30))
            .group_by(col(User.id), col(User.email))
            .order_by(func.count(col(Run.id)).desc(), col(User.id))
            .limit(limit)
        )
        .all()
    )
    data = [
        UserUsage(
            user_id=r[0],
            email=r[1],
            runs=r[2],
            tokens=r[3],
            cost=r[4] if r[5] == r[2] else None,
        )
        for r in rows
    ]
    return UsersUsagePublic(data=data, count=len(data))


@router.get("/recent-failures", response_model=FailuresPublic)
def read_recent_failures(session: SessionDep) -> Any:
    """Retrieve the fifty latest failed runs."""
    rows = session.exec(
        select(Run, Agent.name, User.email)
        .join(AgentVersion, col(Run.agent_version_id) == AgentVersion.id)
        .join(Agent, col(AgentVersion.agent_id) == Agent.id)
        .join(User, col(Run.owner_id) == User.id)
        .where(Run.status == "failed")
        .order_by(col(Run.created_at).desc())
        .limit(50)
    ).all()
    data = [
        FailurePublic(
            id=r.id,
            agent_name=name,
            email=email,
            error=r.error[:300] if r.error else None,
            created_at=r.created_at,
        )
        for r, name, email in rows
    ]
    return FailuresPublic(data=data, count=len(data))


async def probe(name: str, check: Callable[[], Awaitable[bool]]) -> ComponentStatus:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(3):
            healthy = await check()
        detail = "Available" if healthy else "Unavailable or not configured"
    except TimeoutError:
        healthy, detail = False, "Timeout"
    except Exception:
        healthy, detail = False, "Connection failed"
    return ComponentStatus(
        name=name,
        healthy=healthy,
        detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


@router.get("/system", response_model=SystemStatus)
async def read_system() -> Any:
    """Check components independently with bounded timeouts."""
    depth: int | None = None

    async def postgres() -> bool:
        async with async_engine.connect() as conn:
            return (await conn.execute(text("SELECT 1"))).scalar() == 1

    async def redis() -> bool:
        nonlocal depth
        client = get_redis()
        await client.ping()
        depth = int(await client.zcard("arq:queue"))
        return True

    async def minio() -> bool:
        return await to_thread.run_sync(
            get_minio_client().bucket_exists, settings.S3_BUCKET, abandon_on_cancel=True
        )

    async def worker() -> bool:
        return bool(await get_redis().exists("arq:queue:health-check"))

    async def llm() -> bool:
        return bool(settings.LLM_MODEL and settings.LLM_API_KEY)

    components = list(
        await asyncio.gather(
            *(
                probe(name, check)
                for name, check in (
                    ("Postgres", postgres),
                    ("Redis", redis),
                    ("MinIO", minio),
                    ("Worker", worker),
                    ("LLM configuration (not connectivity)", llm),
                )
            )
        )
    )
    return SystemStatus(components=components, queue_depth=depth)
