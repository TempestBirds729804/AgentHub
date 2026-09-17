import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.redis import get_redis
from app.models import Run, RunEventType, RunStatus
from app.services.event_bus import (
    RedisEventPublisher,
    run_stream_key,
    subscribe_run_events,
    terminal_run_payload,
)
from tests.api.routes.test_runs_async import submit


def test_event_replay_terminal_and_retention(client: TestClient) -> None:
    async def check() -> None:
        run = uuid.uuid4()
        redis = get_redis()
        publisher = RedisEventPublisher(redis=redis, run_id=run)
        await publisher.publish(RunEventType.RUN_STARTED, {"run_id": str(run)})
        await publisher.publish(RunEventType.RUN_FINISHED, {"status": "succeeded"})
        events = [
            event async for event in subscribe_run_events(redis=redis, run_id=run)
        ]
        assert [e[0] for e in events] == ["run_started", "run_finished"]
        assert events[1][1]["status"] == "succeeded"
        for _ in range(2000):
            await publisher.publish(RunEventType.MODEL_CHUNK, {"text": "a"})
        assert await redis.xlen(run_stream_key(run)) <= 1100
        assert await redis.ttl(run_stream_key(run)) > 0
        await publisher.close()
        assert await redis.ttl(run_stream_key(run)) > 0

    assert client.portal is not None
    client.portal.call(check)


def test_active_sse_replay_and_database_terminal_fallback(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = submit(client, db, normal_user_token_headers)
    run_id = uuid.UUID(data["id"])
    assert client.portal is not None

    async def publish() -> None:
        assert await terminal_run_payload(run_id) is None
        publisher = RedisEventPublisher(redis=get_redis(), run_id=run_id)
        await publisher.publish(RunEventType.RUN_STARTED, {"run_id": str(run_id)})
        await publisher.publish(RunEventType.RUN_FINISHED, {"status": "succeeded"})

    client.portal.call(publish)
    response = client.get(
        f"/api/v1/runs/{run_id}/stream", headers=normal_user_token_headers
    )
    assert response.status_code == 200
    assert (
        "event: run_started" in response.text and "event: run_finished" in response.text
    )
    run = db.get(Run, run_id)
    assert run is not None
    run.sqlmodel_update({"status": RunStatus.FAILED, "error": "worker disappeared"})
    db.add(run)
    db.commit()

    async def fallback() -> None:
        redis = get_redis()
        monkeypatch.setattr(redis, "xread", AsyncMock(return_value=[]))
        events = [
            event async for event in subscribe_run_events(redis=redis, run_id=run_id)
        ]
        assert events == [
            (
                "run_finished",
                {
                    "run_id": str(run_id),
                    "status": "failed",
                    "error": "worker disappeared",
                },
            )
        ]
        deleted = uuid.uuid4()
        assert await terminal_run_payload(deleted) == {
            "run_id": str(deleted),
            "status": "deleted",
        }

    client.portal.call(fallback)
