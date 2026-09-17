import asyncio
import signal
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from sqlmodel import Session, select

from app.core.async_db import async_session_maker
from app.core.redis import get_redis
from app.models import Run, RunEvent
from app.services.rate_limit import concurrent_runs_key
from app.worker import main, tasks
from tests.api.routes.test_runs_async import submit


@pytest.mark.usefixtures("fake_chat_model")
def test_shutdown_preserves_checkpoint_for_redelivery(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = submit(client, db, normal_user_token_headers)
    started, release = asyncio.Event(), asyncio.Event()
    original = GenericFakeChatModel.ainvoke

    async def slow(self: GenericFakeChatModel, *args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", slow)

    async def check() -> None:
        running = asyncio.create_task(tasks.execute_run_task({}, run["id"]))
        await asyncio.wait_for(started.wait(), 10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        async with async_session_maker() as session:
            saved = await session.get(Run, uuid.UUID(run["id"]))
            assert saved and saved.status == "running" and saved.checkpoint_id
        assert not await get_redis().exists(
            concurrent_runs_key(uuid.UUID(run["owner_id"]))
        )
        release.set()
        assert await tasks.execute_run_task({}, run["id"]) == {"status": "succeeded"}

    assert client.portal is not None
    client.portal.call(check)
    events = db.exec(
        select(RunEvent).where(RunEvent.run_id == uuid.UUID(run["id"]))
    ).all()
    assert any(e.payload.get("resumed") is True for e in events)


def test_worker_infrastructure_failure_and_obsolete_job(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = submit(client, db, normal_user_token_headers)
    assert client.portal is not None
    assert client.portal.call(tasks.execute_run_task, {}, str(uuid.uuid4())) == {
        "status": "not_found"
    }
    assert client.portal.call(
        tasks.execute_run_task, {"job_id": "previous_attempt"}, run["id"]
    ) == {"status": "obsolete"}
    monkeypatch.setattr(
        tasks,
        "execute_run_with_checkpoint",
        AsyncMock(side_effect=ConnectionError("checkpointer unavailable")),
    )
    assert client.portal.call(tasks.execute_run_task, {}, run["id"]) == {
        "status": "failed"
    }
    saved = db.get(Run, uuid.UUID(run["id"]))
    assert saved and saved.finished_at and saved.error == "checkpointer unavailable"


def test_worker_selector_entry_closes_on_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Worker:
        async def async_run(self) -> None:
            raise asyncio.CancelledError()

        def handle_sig(self, value: signal.Signals) -> None:
            assert value == signal.SIGINT
            closed.append("cancel")

        async def close(self) -> None:
            closed.append("close")

    monkeypatch.setattr(main, "create_worker", lambda _settings: Worker())
    main.main()
    assert closed == ["cancel", "close"]


def test_worker_shutdown_hooks(client: TestClient) -> None:
    async def check() -> None:
        await main.startup({})
        redis = get_redis()
        assert await redis.ping()
        await main.shutdown({})
        assert get_redis() is not redis
        assert await get_redis().ping()

    assert client.portal is not None
    client.portal.call(check)
