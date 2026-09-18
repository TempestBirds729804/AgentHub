import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import col, func, select

from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.core.redis import get_redis
from app.models import (
    Agent,
    AgentVersion,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRequestPublic,
    ApprovalRequestsPublic,
    ApprovalStatus,
    Run,
    RunEventType,
    RunStatus,
    get_datetime_utc,
)
from app.services.event_bus import RedisEventPublisher
from app.services.run_service import RunEventRecorder
from app.worker.settings import get_arq_pool
from app.worker.tasks import resume_job_id

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/", response_model=ApprovalRequestsPublic)
def read_approvals(
    session: SessionDep,
    current_user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    status: ApprovalStatus | None = ApprovalStatus.PENDING,
    run_id: uuid.UUID | None = None,
) -> Any:
    """Retrieve approval requests with agent names, filtered by status and run."""
    filters = []
    if not current_user.is_superuser:
        filters.append(ApprovalRequest.owner_id == current_user.id)
    if status is not None:
        filters.append(ApprovalRequest.status == status)
    if run_id is not None:
        filters.append(ApprovalRequest.run_id == run_id)
    count = session.exec(
        select(func.count()).select_from(ApprovalRequest).where(*filters)
    ).one()
    rows = session.exec(
        select(ApprovalRequest, Agent.name)
        .join(Run, col(ApprovalRequest.run_id) == Run.id)
        .join(AgentVersion, col(Run.agent_version_id) == AgentVersion.id)
        .join(Agent, col(AgentVersion.agent_id) == Agent.id)
        .where(*filters)
        .order_by(col(ApprovalRequest.created_at), col(ApprovalRequest.id))
        .offset(skip)
        .limit(limit)
    ).all()
    return ApprovalRequestsPublic(
        data=[
            ApprovalRequestPublic.model_validate(request, update={"agent_name": name})
            for request, name in rows
        ],
        count=count,
    )


@router.get("/{id}", response_model=ApprovalRequestPublic)
def read_approval(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Retrieve an accessible approval request."""
    request = session.get(ApprovalRequest, id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if not current_user.is_superuser and request.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return request


@router.post("/{id}/decide", response_model=ApprovalRequestPublic)
async def decide_approval(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    decision: ApprovalDecision,
) -> Any:
    """Approve or reject a pending tool call and enqueue fully decided runs."""
    request = await session.get(ApprovalRequest, id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if not current_user.is_superuser and request.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    # Serialize every decision in the round and cancellation on the same Run row.
    # Locking only individual requests allows two last decisions to miss each other.
    run = await session.get(Run, request.run_id, with_for_update=True)
    await session.refresh(request, with_for_update=True)
    if (
        run is None
        or run.status != RunStatus.WAITING_APPROVAL
        or request.status != ApprovalStatus.PENDING
    ):
        raise HTTPException(
            status_code=409, detail="Approval request is no longer pending"
        )
    if request.checkpoint_id != run.checkpoint_id or request.thread_id != run.thread_id:
        raise HTTPException(
            status_code=409, detail="Approval request belongs to an obsolete checkpoint"
        )
    reason = (decision.rejection_reason or "").strip()
    if not decision.approved and not reason:
        raise HTTPException(status_code=422, detail="A rejection reason is required")
    request.sqlmodel_update(
        {
            "status": ApprovalStatus.APPROVED
            if decision.approved
            else ApprovalStatus.REJECTED,
            "resolved_by_id": current_user.id,
            "resolved_at": get_datetime_utc(),
            "rejection_reason": None if decision.approved else reason,
        }
    )
    session.add(request)
    await session.flush()
    pending = (
        await session.execute(
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.run_id == run.id,
                ApprovalRequest.status == ApprovalStatus.PENDING,
            )
            .limit(1)
        )
    ).first()
    recorder = RunEventRecorder(session=session, run_id=run.id)
    await recorder.resync_seq()
    payload = {
        "approval_id": str(id),
        "tool_call_id": request.tool_call_id,
        "approved": decision.approved,
        "rejection_reason": request.rejection_reason,
        "resolved_by_id": str(current_user.id),
        "run_id": str(run.id),
    }
    await recorder.record(RunEventType.APPROVAL_RESOLVED, payload=payload)
    if pending is None:
        run.sqlmodel_update(
            {"status": RunStatus.RUNNING, "started_at": get_datetime_utc()}
        )
        session.add(run)
    await session.commit()
    await session.refresh(request)
    if pending is None:
        try:
            pool = await get_arq_pool()
            job = await pool.enqueue_job(
                "resume_run_task", str(run.id), _job_id=resume_job_id(run)
            )
            if job is None:
                raise RuntimeError("Resume job already exists")
        except Exception:
            await session.refresh(run, with_for_update=True)
            if str(run.status) == RunStatus.RUNNING:
                run.sqlmodel_update(
                    {
                        "status": RunStatus.FAILED,
                        "error": "Failed to enqueue approval resume; retry the run",
                        "finished_at": get_datetime_utc(),
                    }
                )
                session.add(run)
                await recorder.resync_seq()
                await recorder.record(
                    RunEventType.RUN_FAILED, payload={"error": run.error}
                )
                await session.commit()
                await session.refresh(run)
            raise HTTPException(
                status_code=503,
                detail="Task queue unavailable; decision saved, retry the run",
            )
    # Decisions are durable even if notification transport is temporarily down.
    try:
        await RedisEventPublisher(redis=get_redis(), run_id=run.id).publish(
            RunEventType.APPROVAL_RESOLVED, payload
        )
    except Exception:
        pass
    return request
