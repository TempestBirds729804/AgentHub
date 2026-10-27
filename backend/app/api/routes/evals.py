import uuid
from decimal import InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import col, func, select

from app.agent.evaluation import validate_assertions
from app.agent.snapshot import AgentSnapshot
from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.core.metrics import evals_total
from app.models import (
    Agent,
    AgentVersion,
    EvalCase,
    EvalCaseCreate,
    EvalCasePublic,
    EvalCasesPublic,
    EvalCaseUpdate,
    EvalComparisonItem,
    EvalComparisonPublic,
    EvalResult,
    EvalResultPublic,
    EvalResultsPublic,
    EvalRun,
    EvalRunCreate,
    EvalRunPublic,
    EvalRunsPublic,
    EvalRunStatus,
    Message,
    get_datetime_utc,
)
from app.worker.settings import get_arq_pool

router = APIRouter(prefix="/evals", tags=["evals"])


def _agent(session: SessionDep, user: CurrentUser, agent_id: uuid.UUID) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    if not user.is_superuser and agent.owner_id != user.id:
        raise HTTPException(403, "Not enough permissions")
    return agent


def _case(session: SessionDep, user: CurrentUser, id: uuid.UUID) -> EvalCase:
    case = session.get(EvalCase, id)
    if case is None:
        raise HTTPException(404, "Eval case not found")
    _agent(session, user, case.agent_id)
    return case


def _batch(session: SessionDep, user: CurrentUser, id: uuid.UUID) -> EvalRun:
    batch = session.get(EvalRun, id)
    if batch is None:
        raise HTTPException(404, "Eval run not found")
    if not user.is_superuser and batch.owner_id != user.id:
        raise HTTPException(403, "Not enough permissions")
    return batch


def _public(session: SessionDep, batch: EvalRun) -> EvalRunPublic:
    version = session.get(AgentVersion, batch.agent_version_id)
    agent = session.get(Agent, version.agent_id) if version else None
    return EvalRunPublic.model_validate(
        batch,
        update={
            "agent_name": agent.name if agent else None,
            "agent_version_number": version.version_number if version else None,
            "pass_rate": batch.passed_count / batch.completed_count
            if batch.completed_count
            else None,
        },
    )


def _validate(case: EvalCaseCreate) -> None:
    message = case.input.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 20000:
        raise HTTPException(400, "input.message must contain 1 to 20000 characters")
    if len(case.assertions) > 30 or len(case.tags) > 30:
        raise HTTPException(400, "At most 30 assertions and tags are allowed")
    try:
        validate_assertions(case.assertions)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise HTTPException(400, str(exc))


@router.get("/cases", response_model=EvalCasesPublic)
def read_cases(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> Any:
    """List evaluation cases belonging to an agent."""
    _agent(session, current_user, agent_id)
    query = select(EvalCase).where(EvalCase.agent_id == agent_id)
    count = session.exec(select(func.count()).select_from(query.subquery())).one()
    cases = session.exec(
        query.order_by(col(EvalCase.created_at).desc(), col(EvalCase.id))
        .offset(skip)
        .limit(limit)
    ).all()
    return EvalCasesPublic(
        data=[EvalCasePublic.model_validate(c) for c in cases], count=count
    )


@router.post("/cases", response_model=EvalCasePublic)
def create_case(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    case_in: EvalCaseCreate,
) -> Any:
    """Create an evaluation case with validated assertions."""
    _agent(session, current_user, agent_id)
    _validate(case_in)
    case = EvalCase.model_validate(case_in, update={"agent_id": agent_id})
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


@router.post("/cases/import", response_model=EvalCasesPublic)
def import_cases(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    cases_in: list[EvalCaseCreate],
) -> Any:
    """Import a validated JSON array of evaluation cases atomically."""
    _agent(session, current_user, agent_id)
    if not 1 <= len(cases_in) <= 100:
        raise HTTPException(400, "Import must contain 1 to 100 cases")
    for case_in in cases_in:
        _validate(case_in)
    cases = [
        EvalCase.model_validate(c, update={"agent_id": agent_id}) for c in cases_in
    ]
    session.add_all(cases)
    session.commit()
    for case in cases:
        session.refresh(case)
    return EvalCasesPublic(
        data=[EvalCasePublic.model_validate(c) for c in cases], count=len(cases)
    )


@router.get("/cases/{id}", response_model=EvalCasePublic)
def read_case(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Retrieve an owned evaluation case."""
    return _case(session, current_user, id)


@router.patch("/cases/{id}", response_model=EvalCasePublic)
def update_case(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    case_in: EvalCaseUpdate,
) -> Any:
    """Update an evaluation case without changing queued snapshots."""
    case = _case(session, current_user, id)
    data = case_in.model_dump(exclude_unset=True)
    try:
        candidate = EvalCaseCreate.model_validate({**case.model_dump(), **data})
    except ValueError:
        raise HTTPException(400, "Invalid evaluation case")
    _validate(candidate)
    case.sqlmodel_update(data, update={"updated_at": get_datetime_utc()})
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


@router.delete("/cases/{id}", response_model=Message)
def delete_case(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """Delete a case only when it is not selected by an active batch."""
    case = _case(session, current_user, id)
    active = session.exec(
        select(EvalRun)
        .join(AgentVersion)
        .where(
            AgentVersion.agent_id == case.agent_id,
            col(EvalRun.status).in_([EvalRunStatus.QUEUED, EvalRunStatus.RUNNING]),
        )
    ).all()
    if any(any(s["id"] == str(id) for s in batch.case_snapshots) for batch in active):
        raise HTTPException(409, "Case is used by an active evaluation")
    session.delete(case)
    session.commit()
    return Message(message="Eval case deleted successfully")


@router.post("/runs", response_model=EvalRunPublic, status_code=202)
async def create_eval_run(
    *, session: AsyncSessionDep, current_user: CurrentUser, run_in: EvalRunCreate
) -> Any:
    """Snapshot selected cases and enqueue a background evaluation."""
    row = (
        await session.execute(
            select(AgentVersion, Agent)
            .join(Agent)
            .where(AgentVersion.id == run_in.agent_version_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Agent version not found")
    version, agent = row
    if not current_user.is_superuser and agent.owner_id != current_user.id:
        raise HTTPException(403, "Not enough permissions")
    try:
        AgentSnapshot.model_validate(version.snapshot)
    except ValueError:
        raise HTTPException(400, "Invalid agent version snapshot")
    cases = list(
        (
            await session.execute(
                select(EvalCase)
                .where(EvalCase.agent_id == agent.id, col(EvalCase.is_active).is_(True))
                .order_by(col(EvalCase.id))
                .with_for_update()
            )
        ).scalars()
    )  # noqa: E712
    if run_in.case_ids is not None:
        selected = set(run_in.case_ids)
        if not selected.issubset({case.id for case in cases}):
            raise HTTPException(
                400, "Selected cases must be active and belong to this agent"
            )
        cases = [case for case in cases if case.id in selected]
    if run_in.tags:
        cases = [case for case in cases if set(run_in.tags).intersection(case.tags)]
    if not 1 <= len(cases) <= 100:
        raise HTTPException(400, "Evaluation must select 1 to 100 active cases")
    for case in cases:
        _validate(EvalCaseCreate.model_validate(case))
    batch = EvalRun(
        owner_id=current_user.id,
        agent_version_id=version.id,
        note=run_in.note,
        case_count=len(cases),
        case_snapshots=[case.model_dump(mode="json") for case in cases],
    )
    metadata = {
        "agent_name": agent.name,
        "agent_version_number": version.version_number,
    }
    session.add(batch)
    await session.commit()
    await session.refresh(batch)
    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job(
            "execute_eval_run_task", str(batch.id), _job_id=f"eval:{batch.id}"
        )
        if job is None:
            raise RuntimeError("Evaluation was not queued")
    except Exception:
        batch.sqlmodel_update(
            {
                "status": EvalRunStatus.FAILED,
                "error": "Evaluation queue unavailable",
                "finished_at": get_datetime_utc(),
            }
        )
        session.add(batch)
        await session.commit()
        evals_total.labels(status="failed").inc()
        raise HTTPException(503, "Evaluation queue unavailable")
    return EvalRunPublic.model_validate(batch, update=metadata)


@router.get("/runs", response_model=EvalRunsPublic)
def read_eval_runs(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> Any:
    """List owned evaluation batches with progress and summaries."""
    query = select(EvalRun)
    if not current_user.is_superuser:
        query = query.where(EvalRun.owner_id == current_user.id)
    if agent_id:
        _agent(session, current_user, agent_id)
        query = query.join(AgentVersion).where(AgentVersion.agent_id == agent_id)
    count = session.exec(select(func.count()).select_from(query.subquery())).one()
    batches = session.exec(
        query.order_by(col(EvalRun.created_at).desc(), col(EvalRun.id))
        .offset(skip)
        .limit(limit)
    ).all()
    return EvalRunsPublic(
        data=[_public(session, batch) for batch in batches], count=count
    )


@router.get("/runs/{id}", response_model=EvalRunPublic)
def read_eval_run(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Retrieve an owned evaluation batch and its statistics."""
    return _public(session, _batch(session, current_user, id))


def _results(session: SessionDep, batch: EvalRun) -> list[EvalResultPublic]:
    names = {str(s["id"]): s["name"] for s in batch.case_snapshots}
    results = session.exec(
        select(EvalResult)
        .where(EvalResult.eval_run_id == batch.id)
        .order_by(col(EvalResult.passed), col(EvalResult.eval_case_id))
    ).all()
    return [
        EvalResultPublic.model_validate(
            r, update={"case_name": names.get(str(r.eval_case_id), str(r.eval_case_id))}
        )
        for r in results
    ]


@router.get("/runs/{id}/results", response_model=EvalResultsPublic)
def read_results(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> Any:
    """List assertion outcomes, failures first, with links to actual runs."""
    results = _results(session, _batch(session, current_user, id))
    return EvalResultsPublic(data=results[skip : skip + limit], count=len(results))


@router.post("/runs/{id}/cancel", response_model=EvalRunPublic)
def cancel_eval_run(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """Cancel an owned queued or running evaluation batch."""
    _batch(session, current_user, id)
    batch = session.exec(
        select(EvalRun)
        .where(EvalRun.id == id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if batch.status in (EvalRunStatus.QUEUED, EvalRunStatus.RUNNING):
        batch.sqlmodel_update(
            {"status": EvalRunStatus.CANCELLED, "finished_at": get_datetime_utc()}
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)
        evals_total.labels(status="cancelled").inc()
    return _public(session, batch)


@router.get("/compare", response_model=EvalComparisonPublic)
def compare_eval_runs(
    session: SessionDep,
    current_user: CurrentUser,
    left_id: uuid.UUID,
    right_id: uuid.UUID,
) -> Any:
    """Compare two owned batches using the union of their case IDs."""
    left, right = (
        _batch(session, current_user, left_id),
        _batch(session, current_user, right_id),
    )
    lmap = {r.eval_case_id: r for r in _results(session, left)}
    rmap = {r.eval_case_id: r for r in _results(session, right)}
    names = {
        uuid.UUID(s["id"]): s["name"]
        for batch in (left, right)
        for s in batch.case_snapshots
    }
    items = []
    for case_id in sorted(names.keys() | lmap.keys() | rmap.keys()):
        lresult, rresult = lmap.get(case_id), rmap.get(case_id)
        verdict = "unchanged"
        if lresult is not None and rresult is not None:
            if lresult.passed and not rresult.passed:
                verdict = "regression"
            elif not lresult.passed and rresult.passed:
                verdict = "improvement"
        items.append(
            EvalComparisonItem(
                case_id=case_id,
                case_name=names.get(case_id, str(case_id)),
                left=lresult,
                right=rresult,
                verdict=verdict,
            )
        )
    return EvalComparisonPublic(
        left=_public(session, left),
        right=_public(session, right),
        items=items,
        regression_count=sum(i.verdict == "regression" for i in items),
        improvement_count=sum(i.verdict == "improvement" for i in items),
    )
