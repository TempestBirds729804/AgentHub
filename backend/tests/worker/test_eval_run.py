import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.async_db import async_session_maker
from app.core.config import settings
from app.models import (
    EvalResult,
    EvalRun,
    Run,
    RunEvent,
    RunEventType,
    RunStatus,
    get_datetime_utc,
)
from app.services import eval_service
from tests.utils.eval import create_eval_batch


@pytest.mark.parametrize("count", [1, 2, 3])
def test_batch_statistics_and_concurrency(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    batch = create_eval_batch(db, count)
    active = peak = 0
    monkeypatch.setattr(settings, "EVAL_CONCURRENCY", 2)

    async def execute(**kwargs: Any) -> Run:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
            run = kwargs["run"]
            if run.input["message"] == "input-2":
                raise RuntimeError("One case broke")
            run.sqlmodel_update(
                {
                    "status": RunStatus.SUCCEEDED,
                    "output": {"content": "fake reply"},
                    "duration_ms": 100,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost_usd": Decimal(".01"),
                    "finished_at": get_datetime_utc(),
                }
            )
            kwargs["session"].add(run)
            await kwargs["session"].commit()
            return run
        finally:
            active -= 1

    monkeypatch.setattr(eval_service, "execute_run_with_checkpoint", execute)
    assert client.portal
    client.portal.call(eval_service.execute_eval_run_task, {}, str(batch.id))
    db.refresh(batch)
    results = db.exec(
        select(EvalResult).where(EvalResult.eval_run_id == batch.id)
    ).all()
    assert batch.status == "succeeded" and batch.completed_count == count
    assert batch.passed_count == min(2, count)
    assert batch.avg_duration_ms == batch.p95_duration_ms == 100
    assert batch.total_prompt_tokens == min(2, count) * 10
    assert batch.total_cost_usd == (None if count == 3 else Decimal(".01") * count)
    assert peak == min(2, count)
    assert all(result.run_id and db.get(Run, result.run_id) for result in results)
    if count == 3:
        assert any(not r.passed and r.error == "One case broke" for r in results)
    client.portal.call(eval_service.execute_eval_run_task, {}, str(batch.id))
    assert (
        len(db.exec(select(EvalResult).where(EvalResult.eval_run_id == batch.id)).all())
        == count
    )


@pytest.mark.usefixtures("fake_chat_model")
def test_real_graph_and_running_idempotence(client: TestClient, db: Session) -> None:
    batch = create_eval_batch(db, 1)
    assert client.portal
    client.portal.call(eval_service.execute_eval_run_task, {}, str(batch.id))
    db.refresh(batch)
    assert batch.completed_count == batch.passed_count == 1
    batch.status = "running"
    db.add(batch)
    db.commit()
    assert client.portal.call(
        eval_service.execute_eval_run_task, {}, str(batch.id)
    ) == {"status": "running"}


def test_cancel_active_batch(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = create_eval_batch(db, 3)
    started = asyncio.Event()

    async def slow(**_kwargs: Any) -> None:
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(eval_service, "execute_run_with_checkpoint", slow)

    async def check() -> None:
        task = asyncio.create_task(
            eval_service.execute_eval_run_task({}, str(batch.id))
        )
        await asyncio.wait_for(started.wait(), 5)
        async with async_session_maker() as session:
            current = await session.get(EvalRun, batch.id)
            assert current
            current.status = "cancelled"
            session.add(current)
            await session.commit()
        await asyncio.wait_for(task, 5)

    assert client.portal
    client.portal.call(check)
    db.refresh(batch)
    assert batch.status == "cancelled"
    results = db.exec(
        select(EvalResult).where(EvalResult.eval_run_id == batch.id)
    ).all()
    assert results and all(not r.passed for r in results)


@pytest.mark.parametrize("mode", ["failed", "tool_error", "waiting_approval"])
def test_failures_never_pass(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    batch = create_eval_batch(db, 1)

    async def execute(**kwargs: Any) -> Run:
        run = kwargs["run"]
        run.sqlmodel_update(
            {
                "status": "succeeded" if mode == "tool_error" else mode,
                "output": {"content": "fake reply"},
                "error": "failed" if mode == "failed" else None,
            }
        )
        kwargs["session"].add(run)
        if mode == "tool_error":
            kwargs["session"].add(
                RunEvent(
                    run_id=run.id,
                    seq=1,
                    event_type=RunEventType.TOOL_RESULT,
                    payload={"ok": False, "tool": "docs"},
                )
            )
        await kwargs["session"].commit()
        return run

    monkeypatch.setattr(eval_service, "execute_run_with_checkpoint", execute)
    assert client.portal
    client.portal.call(eval_service.execute_eval_run_task, {}, str(batch.id))
    result = db.exec(select(EvalResult).where(EvalResult.eval_run_id == batch.id)).one()
    assert not result.passed and result.error


def test_stale_and_missing_batches(client: TestClient, db: Session) -> None:
    import uuid

    batch = create_eval_batch(db, 1)
    batch.created_at = get_datetime_utc() - timedelta(hours=3)
    db.add(batch)
    db.commit()
    assert client.portal
    assert client.portal.call(
        eval_service.execute_eval_run_task, {}, str(uuid.uuid4())
    ) == {"status": "not_found"}
    assert client.portal.call(eval_service.reap_stale_eval_runs, {}) >= 1
    db.refresh(batch)
    assert batch.status == "failed" and batch.finished_at
