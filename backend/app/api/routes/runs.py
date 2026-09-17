import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import ValidationError
from sqlalchemy import text
from sqlmodel import col, func, select

from app.agent.exceptions import AgentError, ModelNotConfiguredError
from app.agent.models import get_provider
from app.agent.snapshot import AgentSnapshot
from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.core.redis import get_redis
from app.models import (
    Agent,
    AgentVersion,
    Conversation,
    Run,
    RunCreate,
    RunEvent,
    RunEventPublic,
    RunEventsPublic,
    RunEventType,
    RunPublic,
    RunsPublic,
    RunStatus,
    get_datetime_utc,
)
from app.services.event_bus import (
    TERMINAL_STATUSES,
    RedisEventPublisher,
    cancel_key,
    run_stream_key,
    subscribe_run_events,
)
from app.services.rate_limit import release_run_slot, try_acquire_run_slot
from app.services.run_service import RunEventRecorder, execute_run
from app.worker.settings import JOB_TIMEOUT_SECONDS, get_arq_pool
from app.worker.tasks import run_job_id, run_lock_key

router = APIRouter(prefix="/runs", tags=["runs"])
logger = logging.getLogger(__name__)


async def _get_async_owned_run(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    run_id: uuid.UUID,
    lock: bool = False,
) -> Run:
    run = await session.get(Run, run_id, with_for_update=lock)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not current_user.is_superuser and run.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return run


async def _validate_async_conversation(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    conversation_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
) -> None:
    if conversation_id is None:
        return
    conversation = await session.get(
        Conversation, conversation_id, with_for_update=True
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not current_user.is_superuser and conversation.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    if conversation.agent_id != agent_id:
        raise HTTPException(
            status_code=400, detail="Conversation belongs to another agent"
        )
    active = (
        await session.execute(
            select(Run.id)
            .where(
                Run.conversation_id == conversation_id,
                Run.id != run_id,
                col(Run.status).in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
            .limit(1)
        )
    ).first()
    if active is not None:
        raise HTTPException(status_code=409, detail="Conversation has an active run")


def _async_snapshot(version: AgentVersion) -> AgentSnapshot:
    try:
        snapshot = AgentSnapshot.model_validate(version.snapshot)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid agent version snapshot")
    if snapshot.timeout_seconds > JOB_TIMEOUT_SECONDS:
        raise HTTPException(
            status_code=400, detail="Async run timeout must not exceed 900 seconds"
        )
    return snapshot


async def _enqueue_run(*, session: AsyncSessionDep, run: Run) -> RunPublic:
    redis = get_redis()
    run_id, owner_id = run.id, run.owner_id
    try:
        if not await try_acquire_run_slot(redis=redis, user_id=owner_id, run_id=run_id):
            await session.rollback()
            raise HTTPException(status_code=429, detail="Concurrent run limit reached")
        await redis.delete(cancel_key(run_id), run_stream_key(run_id))
        if not run.thread_id or not run.thread_id.startswith(f"run-{run_id}-"):
            run.sqlmodel_update({"thread_id": f"run-{run_id}-{run.retry_count}"})
        session.add(run)
        await session.commit()
        await session.refresh(run)
        response = RunPublic.model_validate(run)
        pool = await get_arq_pool()
        job = await pool.enqueue_job(
            "execute_run_task", str(run_id), _job_id=run_job_id(run)
        )
        if job is None:
            raise RuntimeError("Run job already exists")
        return response
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        current = await session.get(Run, run_id, with_for_update=True)
        if current is None:
            current = run
        current.sqlmodel_update(
            {
                "status": RunStatus.FAILED,
                "error": f"Failed to enqueue job: {type(exc).__name__}"[:4000],
                "finished_at": get_datetime_utc(),
            }
        )
        session.add(current)
        await session.commit()
        await session.refresh(current)
        try:
            await release_run_slot(redis=redis, user_id=owner_id, run_id=run_id)
        except Exception:
            logger.warning("Queue unavailable; Run %s slot will expire", run_id)
        raise HTTPException(
            status_code=503, detail="Task queue unavailable, please retry"
        )


@router.post("/async", response_model=RunPublic, status_code=202)
async def create_async_run(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    run_in: RunCreate,
) -> Any:
    """Submit long-running production work; use conversation streaming for Playground debugging."""
    version, agent = await _get_owned_version(
        session=session, current_user=current_user, version_id=run_in.agent_version_id
    )
    _async_snapshot(version)
    message = run_in.input.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 20000:
        raise HTTPException(
            status_code=422, detail="input.message must contain 1 to 20000 characters"
        )
    run = Run.model_validate(run_in, update={"owner_id": current_user.id})
    await _validate_async_conversation(
        session=session,
        current_user=current_user,
        conversation_id=run.conversation_id,
        agent_id=agent.id,
        run_id=run.id,
    )
    return await _enqueue_run(session=session, run=run)


@router.post("/{id}/cancel", response_model=RunPublic)
async def cancel_run(
    *, session: AsyncSessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """Cancel async work at the next node boundary; stop Playground streams at their source."""
    run = await _get_async_owned_run(
        session=session, current_user=current_user, run_id=id, lock=True
    )
    if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
        raise HTTPException(
            status_code=400, detail="Only queued or running runs can be cancelled"
        )
    if not run.thread_id or not run.thread_id.startswith(f"run-{id}-"):
        raise HTTPException(
            status_code=400, detail="Only async runs can be cancelled here"
        )
    queued = run.status == RunStatus.QUEUED
    # DB status is authoritative even if Redis is unavailable during cancellation.
    try:
        await get_redis().set(cancel_key(id), "1", ex=3600)
    except Exception:
        logger.warning("Cancellation flag unavailable for Run %s; using DB status", id)
    run.sqlmodel_update(
        {"status": RunStatus.CANCELLED, "finished_at": get_datetime_utc()}
    )
    session.add(run)
    if queued:
        recorder = RunEventRecorder(session=session, run_id=id)
        await recorder.resync_seq()
        await recorder.record(
            RunEventType.RUN_FINISHED, payload={"status": "cancelled"}
        )
    await session.commit()
    await session.refresh(run)
    if queued:
        try:
            await release_run_slot(redis=get_redis(), user_id=run.owner_id, run_id=id)
            publisher = RedisEventPublisher(redis=get_redis(), run_id=id)
            await publisher.publish(
                RunEventType.RUN_FINISHED, {"run_id": str(id), "status": "cancelled"}
            )
            await publisher.close()
        except Exception:
            logger.warning("Redis cleanup unavailable for cancelled Run %s", id)
    return run


@router.post("/{id}/retry", response_model=RunPublic, status_code=202)
async def retry_run(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    fresh: bool = False,
) -> Any:
    """Retry failed/cancelled work in place, optionally starting with a fresh checkpoint thread."""
    # Same lock as the worker: a cancelled node must finish closing its clients
    # before a fresh attempt can replace its status/checkpoint/Redis stream.
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": run_lock_key(id)}
    )
    run = await _get_async_owned_run(
        session=session, current_user=current_user, run_id=id, lock=True
    )
    if not locked:
        raise HTTPException(
            status_code=409, detail="Run is still stopping, please retry shortly"
        )
    if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise HTTPException(
            status_code=400, detail="Only failed or cancelled runs can be retried"
        )
    if run.retry_count >= 3:
        raise HTTPException(status_code=400, detail="Run retry limit of 3 reached")
    version, agent = await _get_owned_version(
        session=session, current_user=current_user, version_id=run.agent_version_id
    )
    _async_snapshot(version)
    await _validate_async_conversation(
        session=session,
        current_user=current_user,
        conversation_id=run.conversation_id,
        agent_id=agent.id,
        run_id=id,
    )
    # Acquisition is idempotent: a crash-leaked reservation is reused, and a
    # Redis outage is handled by _enqueue_run's durable 503 failure path.
    run.sqlmodel_update(
        {
            "status": RunStatus.QUEUED,
            "error": None,
            "output": None,
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "retry_count": run.retry_count + 1,
        }
    )
    if fresh:
        run.sqlmodel_update(
            {
                "checkpoint_id": None,
                "thread_id": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": None,
            }
        )
    return await _enqueue_run(session=session, run=run)


@router.get("/{id}/stream", response_class=StreamingResponse)
async def stream_run_events(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """Replay buffered async Run events and stream new progress with authenticated fetch SSE."""
    run = await _get_async_owned_run(
        session=session, current_user=current_user, run_id=id
    )
    terminal = (
        {"run_id": str(id), "status": run.status, "error": run.error}
        if run.status in TERMINAL_STATUSES
        else None
    )
    await session.commit()
    if terminal is None:
        try:
            await get_redis().ping()
        except Exception:
            raise HTTPException(
                status_code=503, detail="Event stream unavailable, please retry"
            )

    async def events() -> AsyncGenerator[str]:
        if terminal is not None:
            yield f"event: run_finished\ndata: {json.dumps(terminal)}\n\n"
            return
        async for event_type, payload in subscribe_run_events(
            redis=get_redis(), run_id=id
        ):
            yield f"event: {event_type}\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _get_owned_version(
    *, session: AsyncSessionDep, current_user: CurrentUser, version_id: uuid.UUID
) -> tuple[AgentVersion, Agent]:
    result = await session.execute(
        select(AgentVersion, Agent).join(Agent).where(AgentVersion.id == version_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent version not found")
    version, agent = row
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return version, agent


def _get_owned_run(
    *, session: SessionDep, current_user: CurrentUser, run_id: uuid.UUID
) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not current_user.is_superuser and run.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return run


@router.post("/", response_model=RunPublic)
async def create_run(
    *, session: AsyncSessionDep, current_user: CurrentUser, run_in: RunCreate
) -> Any:
    """Create and execute an agent run, blocking until it finishes."""
    version, agent = await _get_owned_version(
        session=session, current_user=current_user, version_id=run_in.agent_version_id
    )
    if run_in.conversation_id is not None:
        conversation = await session.get(Conversation, run_in.conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if not current_user.is_superuser and conversation.owner_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not enough permissions")
        if conversation.agent_id != agent.id:
            raise HTTPException(
                status_code=400, detail="Conversation belongs to another agent"
            )
    try:
        snapshot = AgentSnapshot.model_validate(version.snapshot)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid agent version snapshot")
    message = run_in.input.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(
            status_code=422, detail="input.message must be a non-empty string"
        )
    try:
        get_provider()
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Capture response metadata before execution: rollback expires ORM objects.
    metadata = {
        "agent_name": agent.name,
        "agent_version_number": version.version_number,
    }
    run = Run.model_validate(run_in, update={"owner_id": current_user.id})
    session.add(run)
    await session.commit()
    await session.refresh(run)
    try:
        await execute_run(
            session=session,
            run=run,
            snapshot=snapshot,
            initial_messages=[HumanMessage(content=message)],
        )
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except AgentError:
        await session.refresh(run)
    return RunPublic.model_validate(run, update=metadata)


@router.get("/", response_model=RunsPublic)
def read_runs(
    session: SessionDep,
    current_user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    agent_version_id: uuid.UUID | None = None,
    status: RunStatus | None = None,
) -> Any:
    """Retrieve runs filtered by agent version and status."""
    filters = []
    if not current_user.is_superuser:
        filters.append(Run.owner_id == current_user.id)
    if agent_version_id is not None:
        filters.append(Run.agent_version_id == agent_version_id)
    if status is not None:
        filters.append(Run.status == status)
    count = session.exec(select(func.count()).select_from(Run).where(*filters)).one()
    rows = session.exec(
        select(Run, Agent.name, AgentVersion.version_number)
        .join(AgentVersion, col(Run.agent_version_id) == col(AgentVersion.id))
        .join(Agent, col(AgentVersion.agent_id) == col(Agent.id))
        .where(*filters)
        .order_by(col(Run.created_at).desc(), col(Run.id).desc())
        .offset(skip)
        .limit(limit)
    ).all()
    return RunsPublic(
        data=[
            RunPublic.model_validate(
                run, update={"agent_name": name, "agent_version_number": version_number}
            )
            for run, name, version_number in rows
        ],
        count=count,
    )


@router.get("/{id}", response_model=RunPublic)
def read_run(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Get a run by ID."""
    run = _get_owned_run(session=session, current_user=current_user, run_id=id)
    name, version_number = session.exec(
        select(Agent.name, AgentVersion.version_number)
        .join(AgentVersion, col(Agent.id) == col(AgentVersion.agent_id))
        .where(AgentVersion.id == run.agent_version_id)
    ).one()
    return RunPublic.model_validate(
        run, update={"agent_name": name, "agent_version_number": version_number}
    )


@router.get("/{id}/events", response_model=RunEventsPublic)
def read_events(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Any:
    """Retrieve run events ordered by sequence number."""
    _get_owned_run(session=session, current_user=current_user, run_id=id)
    count = session.exec(
        select(func.count()).select_from(RunEvent).where(RunEvent.run_id == id)
    ).one()
    events = session.exec(
        select(RunEvent)
        .where(RunEvent.run_id == id)
        .order_by(col(RunEvent.seq))
        .offset(skip)
        .limit(limit)
    ).all()
    return RunEventsPublic(
        data=[RunEventPublic.model_validate(event) for event in events], count=count
    )
