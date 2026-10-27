import asyncio
import uuid
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlmodel import col, select

from app.agent.evaluation import EvaluationContext, evaluate_case
from app.core.async_db import async_session_maker
from app.core.config import settings
from app.core.metrics import evals_total
from app.core.redis import get_redis
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    EvalCase,
    EvalResult,
    EvalRun,
    EvalRunStatus,
    Run,
    RunEvent,
    RunEventType,
    RunStatus,
    RunTrigger,
    get_datetime_utc,
)
from app.services.event_bus import RedisEventPublisher
from app.services.run_service import execute_run_with_checkpoint


async def _execute_case(*, batch: EvalRun, snapshot: dict[str, Any]) -> None:
    case = EvalCase.model_validate(snapshot)
    async with async_session_maker() as session:
        current = await session.get(EvalRun, batch.id)
        if current is None or current.status != EvalRunStatus.RUNNING:
            return
        run = Run(
            owner_id=batch.owner_id,
            agent_version_id=batch.agent_version_id,
            input=case.input,
            trigger=RunTrigger.EVAL,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id
        publisher = RedisEventPublisher(redis=get_redis(), run_id=run_id)
        passed, score = False, 0.0
        details: list[dict[str, Any]] = []
        error: str | None = None
        cancelled = False
        try:
            await execute_run_with_checkpoint(
                session=session, run=run, publisher=publisher
            )
            await session.refresh(run)
            if run.status == RunStatus.WAITING_APPROVAL:
                # Offline evaluation never approves a high-risk side effect.
                raise ValueError(
                    "Evaluation requires human approval; case cannot complete offline"
                )
            events = list(
                (
                    await session.execute(
                        select(RunEvent).where(RunEvent.run_id == run_id)
                    )
                ).scalars()
            )
            tool_failed = any(
                e.event_type == RunEventType.TOOL_RESULT
                and e.payload.get("ok") is False
                for e in events
            )
            context = EvaluationContext(
                output=str((run.output or {}).get("content", "")),
                run=run,
                tool_names_called=[
                    str(e.payload.get("tool", ""))
                    for e in events
                    if e.event_type == RunEventType.TOOL_CALLED
                ],
                duration_ms=run.duration_ms or 0,
                cost_usd=run.cost_usd,
            )
            passed, score, details = await evaluate_case(case=case, ctx=context)
            passed = passed and run.status == RunStatus.SUCCEEDED and not tool_failed
            error = "Tool execution failed" if tool_failed else run.error
        except asyncio.CancelledError:
            cancelled = True
            error = "Evaluation cancelled or timed out"
        except Exception as exc:
            error = (str(exc) or type(exc).__name__)[:2000]
        finally:
            with suppress(Exception):
                await publisher.close()
        await session.rollback()
        persisted_run = await session.get(Run, run_id)
        if persisted_run is None:
            return
        run = persisted_run
        if run.status not in (
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            run.sqlmodel_update(
                {
                    "status": RunStatus.CANCELLED if cancelled else RunStatus.FAILED,
                    "error": error,
                    "finished_at": get_datetime_utc(),
                }
            )
            session.add(run)
            await session.execute(
                update(ApprovalRequest)
                .where(
                    col(ApprovalRequest.run_id) == run_id,
                    col(ApprovalRequest.status) == ApprovalStatus.PENDING,
                )
                .values(status=ApprovalStatus.EXPIRED, resolved_at=get_datetime_utc())
            )
        session.add(
            EvalResult(
                eval_run_id=batch.id,
                eval_case_id=case.id,
                run_id=run_id,
                passed=passed and not cancelled,
                score=score,
                actual_output=str((run.output or {}).get("content", ""))[:20000],
                assertion_results=details,
                error=error[:2000] if error else None,
                duration_ms=run.duration_ms,
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                cost_usd=run.cost_usd,
            )
        )
        await session.execute(
            update(EvalRun)
            .where(col(EvalRun.id) == batch.id)
            .values(completed_count=col(EvalRun.completed_count) + 1)
        )
        await session.commit()
        if cancelled:
            raise asyncio.CancelledError


async def finalize_eval_run(
    *, eval_run_id: uuid.UUID, error: str | None = None
) -> None:
    async with async_session_maker() as session:
        batch = await session.get(EvalRun, eval_run_id, with_for_update=True)
        if batch is None:
            return
        results = list(
            (
                await session.execute(
                    select(EvalResult).where(EvalResult.eval_run_id == eval_run_id)
                )
            ).scalars()
        )
        durations = sorted(r.duration_ms for r in results if r.duration_ms is not None)
        # A partial sum is not the total: any unknown price makes the total unknown.
        costs = [r.cost_usd for r in results if r.cost_usd is not None]
        status = batch.status
        was_running = status == EvalRunStatus.RUNNING
        if status == EvalRunStatus.RUNNING:
            status = (
                EvalRunStatus.FAILED
                if error or len(results) != batch.case_count
                else EvalRunStatus.SUCCEEDED
            )
        batch.sqlmodel_update(
            {
                "status": status,
                "error": error or batch.error,
                "completed_count": len(results),
                "passed_count": sum(r.passed for r in results),
                "total_prompt_tokens": sum(r.prompt_tokens for r in results),
                "total_completion_tokens": sum(r.completion_tokens for r in results),
                "total_cost_usd": sum(costs, Decimal(0))
                if costs and len(costs) == len(results)
                else None,
                "avg_duration_ms": int(sum(durations) / len(durations))
                if durations
                else None,
                "p95_duration_ms": durations[
                    min(int(len(durations) * 0.95), len(durations) - 1)
                ]
                if durations
                else None,
                "finished_at": get_datetime_utc(),
            }
        )
        session.add(batch)
        await session.commit()
        await session.refresh(batch)
        if was_running:
            evals_total.labels(
                status=str(getattr(batch.status, "value", batch.status))
            ).inc()


async def execute_eval_run_task(
    _ctx: dict[str, Any], eval_run_id: str
) -> dict[str, Any]:
    batch_id = uuid.UUID(eval_run_id)
    async with async_session_maker() as session:
        batch = await session.get(EvalRun, batch_id, with_for_update=True)
        if batch is None:
            return {"status": "not_found"}
        if batch.status != EvalRunStatus.QUEUED:
            return {"status": batch.status}
        batch.sqlmodel_update(
            {"status": EvalRunStatus.RUNNING, "started_at": get_datetime_utc()}
        )
        session.add(batch)
        await session.commit()
        await session.refresh(batch)
    semaphore = asyncio.Semaphore(
        max(
            1,
            min(settings.EVAL_CONCURRENCY, settings.AGENT_MAX_CONCURRENT_RUNS_PER_USER),
        )
    )

    async def one(snapshot: dict[str, Any]) -> None:
        async with semaphore:
            await _execute_case(batch=batch, snapshot=snapshot)

    tasks = [asyncio.create_task(one(snapshot)) for snapshot in batch.case_snapshots]
    error = None
    try:
        pending = set(tasks)
        while pending:
            _, pending = await asyncio.wait(pending, timeout=0.5)
            async with async_session_maker() as session:
                current = await session.get(EvalRun, batch_id)
                if current is None or current.status != EvalRunStatus.RUNNING:
                    for task in pending:
                        task.cancel()
                    break
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        if any(isinstance(result, Exception) for result in outcomes):
            error = "One or more evaluation results could not be persisted"
    except asyncio.CancelledError:
        error = "Evaluation worker interrupted or timed out"
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await finalize_eval_run(eval_run_id=batch_id, error=error)
    return {"status": "finished"}


async def reap_stale_eval_runs(_ctx: dict[str, Any]) -> int:
    cutoff = get_datetime_utc() - timedelta(hours=2)
    async with async_session_maker() as session:
        batches = list(
            (
                await session.execute(
                    select(EvalRun).where(
                        col(EvalRun.status).in_(
                            [EvalRunStatus.RUNNING, EvalRunStatus.QUEUED]
                        ),
                        col(EvalRun.created_at) < cutoff,
                    )
                )
            ).scalars()
        )
        ids = [batch.id for batch in batches]
    for batch_id in ids:
        async with async_session_maker() as session:
            batch = await session.get(EvalRun, batch_id, with_for_update=True)
            if batch is not None and batch.status in (
                EvalRunStatus.QUEUED,
                EvalRunStatus.RUNNING,
            ):
                batch.sqlmodel_update(
                    {
                        "status": EvalRunStatus.FAILED,
                        "error": "Stale evaluation",
                        "finished_at": get_datetime_utc(),
                    }
                )
                session.add(batch)
                await session.commit()
                evals_total.labels(status="failed").inc()
    return len(ids)
