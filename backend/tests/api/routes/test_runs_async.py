import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from arq.jobs import Job
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlmodel import Session, select

from app.core.config import settings
from app.core.redis import get_redis
from app.models import (
    Conversation,
    ConvMessage,
    Run,
    RunEvent,
    RunStatus,
    get_datetime_utc,
)
from app.services.rate_limit import concurrent_runs_key
from app.worker.settings import get_arq_pool
from app.worker.tasks import execute_run_task, reap_stale_runs
from tests.api.routes.test_runs import version_for_user

URL = f"{settings.API_V1_STR}/runs"


def submit(client: TestClient, db: Session, headers: dict[str, str]) -> dict[str, Any]:
    version = version_for_user(db)
    response = client.post(
        URL + "/async",
        headers=headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "hello"},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.usefixtures("fake_chat_model")
def test_queue_and_worker(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
) -> None:
    run = submit(client, db, normal_user_token_headers)
    assert run["status"] == "queued"
    assert client.portal is not None

    async def execute() -> None:
        pool = await get_arq_pool()
        job = Job(f"agenthub:run:{run['id']}:attempt:0", pool)
        info = await job.info()
        assert info is not None and info.args == (run["id"],)
        assert await execute_run_task({}, run["id"]) == {"status": "succeeded"}
        assert await execute_run_task({}, run["id"]) == {"status": "succeeded"}
        assert not await get_redis().exists(
            concurrent_runs_key(uuid.UUID(run["owner_id"]))
        )

    client.portal.call(execute)
    detail = client.get(f"{URL}/{run['id']}", headers=normal_user_token_headers).json()
    assert detail["output"]["content"] == "fake reply"
    assert detail["checkpoint_id"] and detail["thread_id"]
    events = db.exec(
        select(RunEvent).where(RunEvent.run_id == uuid.UUID(run["id"]))
    ).all()
    assert len([e for e in events if e.event_type == "run_started"]) == 1
    assert not any(e.event_type == "model_chunk" for e in events)
    terminal = client.get(
        f"{URL}/{run['id']}/stream", headers=normal_user_token_headers
    )
    assert "event: run_finished" in terminal.text
    assert terminal.headers["x-accel-buffering"] == "no"
    assert (
        client.post(
            f"{URL}/{run['id']}/retry", headers=normal_user_token_headers
        ).status_code
        == 400
    )


def test_limits_cancel_retry(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
) -> None:
    runs = [submit(client, db, normal_user_token_headers) for _ in range(3)]
    response = client.post(
        URL + "/async",
        headers=normal_user_token_headers,
        json={
            "agent_version_id": runs[0]["agent_version_id"],
            "input": {"message": "fourth"},
        },
    )
    assert response.status_code == 429
    run_id = runs[0]["id"]
    assert (
        client.post(f"{URL}/{run_id}/cancel", headers=normal_user_token_headers).json()[
            "status"
        ]
        == "cancelled"
    )
    assert client.portal is not None
    assert client.portal.call(execute_run_task, {}, run_id)["status"] == "cancelled"
    for count in range(1, 4):
        retried = client.post(
            f"{URL}/{run_id}/retry?fresh=true", headers=normal_user_token_headers
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["retry_count"] == count
        assert retried.json()["checkpoint_id"] is None
        client.post(f"{URL}/{run_id}/cancel", headers=normal_user_token_headers)
    assert (
        client.post(
            f"{URL}/{run_id}/retry", headers=normal_user_token_headers
        ).status_code
        == 400
    )


def test_async_validation_and_permissions(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    version = version_for_user(db)
    version.snapshot = {**version.snapshot, "timeout_seconds": 901}
    db.add(version)
    db.commit()
    assert (
        client.post(
            URL + "/async",
            headers=normal_user_token_headers,
            json={
                "agent_version_id": str(version.id),
                "input": {"message": "hi"},
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            URL + "/async",
            headers=normal_user_token_headers,
            json={
                "agent_version_id": str(uuid.uuid4()),
                "input": {"message": "hi"},
            },
        ).status_code
        == 404
    )
    version.snapshot = {**version.snapshot, "timeout_seconds": 300}
    db.add(version)
    db.commit()
    response = client.post(
        URL + "/async",
        headers=superuser_token_headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "hi"},
        },
    )
    run_id = response.json()["id"]
    for suffix in ("cancel", "retry"):
        assert (
            client.post(
                f"{URL}/{run_id}/{suffix}", headers=normal_user_token_headers
            ).status_code
            == 403
        )
    assert (
        client.get(
            f"{URL}/{run_id}/stream", headers=normal_user_token_headers
        ).status_code
        == 403
    )


def test_enqueue_failure(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import runs

    async def unavailable() -> Any:
        raise ConnectionError("redis stopped")

    monkeypatch.setattr(runs, "get_arq_pool", unavailable)
    version = version_for_user(db)
    response = client.post(
        URL + "/async",
        headers=normal_user_token_headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "hi"},
        },
    )
    assert response.status_code == 503
    run = db.exec(select(Run).where(Run.agent_version_id == version.id)).one()
    assert run.status == "failed" and "enqueue" in (run.error or "")
    assert client.portal is not None

    async def check() -> None:
        assert not await get_redis().exists(concurrent_runs_key(run.owner_id))

    client.portal.call(check)


def test_reap_interrupted_run(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
) -> None:
    data = submit(client, db, normal_user_token_headers)
    run = db.get(Run, uuid.UUID(data["id"]))
    assert run is not None
    run.status = RunStatus.RUNNING
    run.started_at = get_datetime_utc() - timedelta(seconds=1300)
    db.add(run)
    db.commit()
    assert client.portal is not None
    assert client.portal.call(reap_stale_runs, {}) == 1
    db.refresh(run)
    assert run.status == "failed" and run.finished_at


@pytest.mark.usefixtures("fake_chat_model")
def test_running_cancel_and_duplicate_delivery(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = submit(client, db, normal_user_token_headers)
    original = GenericFakeChatModel.ainvoke
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(self: GenericFakeChatModel, *args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", slow)
    assert client.portal is not None
    future = client.portal.start_task_soon(execute_run_task, {}, run["id"])
    client.portal.call(asyncio.wait_for, started.wait(), 10)
    assert (
        client.portal.call(execute_run_task, {}, run["id"])["status"]
        == "already_running"
    )
    assert (
        client.post(
            f"{URL}/{run['id']}/cancel", headers=normal_user_token_headers
        ).json()["status"]
        == "cancelled"
    )
    assert (
        client.post(
            f"{URL}/{run['id']}/retry", headers=normal_user_token_headers
        ).status_code
        == 409
    )
    client.portal.call(release.set)
    assert future.result(timeout=10)["status"] == "cancelled"


def test_async_conversation_history_and_validation(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = version_for_user(db)
    conversation = client.post(
        "/api/v1/conversations/",
        headers=normal_user_token_headers,
        json={"agent_id": str(version.agent_id), "title": "Async history"},
    ).json()
    body = {
        "agent_version_id": str(version.id),
        "conversation_id": conversation["id"],
        "input": {"message": "first"},
    }
    contexts: list[list[str]] = []
    original = GenericFakeChatModel.ainvoke

    async def capture(self: GenericFakeChatModel, messages: Any, **kwargs: Any) -> Any:
        contexts.append([m.content for m in messages if m.type != "system"])
        return await original(self, messages, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", capture)
    fake_chat_model.messages = iter(
        [AIMessage(content="one"), AIMessage(content="two")]
    )
    first = client.post(URL + "/async", headers=normal_user_token_headers, json=body)
    assert first.status_code == 202, first.text
    assert (
        client.post(
            URL + "/async", headers=normal_user_token_headers, json=body
        ).status_code
        == 409
    )
    assert client.portal is not None
    assert (
        client.portal.call(execute_run_task, {}, first.json()["id"])["status"]
        == "succeeded"
    )
    body["input"] = {"message": "second"}
    second = client.post(URL + "/async", headers=normal_user_token_headers, json=body)
    assert second.status_code == 202
    assert (
        client.portal.call(execute_run_task, {}, second.json()["id"])["status"]
        == "succeeded"
    )
    assert contexts == [["first"], ["first", "one", "second"]]
    messages = db.exec(
        select(ConvMessage)
        .where(ConvMessage.conversation_id == uuid.UUID(conversation["id"]))
        .order_by(ConvMessage.seq)
    ).all()
    assert [m.content for m in messages] == ["first", "one", "second", "two"]
    assert first.json()["thread_id"] != second.json()["thread_id"]
    other = version_for_user(db)
    assert (
        client.post(
            URL + "/async",
            headers=normal_user_token_headers,
            json={**body, "agent_version_id": str(other.id)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            URL + "/async",
            headers=normal_user_token_headers,
            json={**body, "conversation_id": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    foreign = client.post(
        "/api/v1/conversations/",
        headers=superuser_token_headers,
        json={"agent_id": str(version.agent_id), "title": "Foreign"},
    ).json()
    assert (
        client.post(
            URL + "/async",
            headers=normal_user_token_headers,
            json={**body, "conversation_id": foreign["id"]},
        ).status_code
        == 403
    )
    assert db.get(Conversation, uuid.UUID(conversation["id"])) is not None


def test_queue_outage_retry_cancel_and_stream(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import runs

    queued = submit(client, db, normal_user_token_headers)

    class OfflineRedis:
        async def ping(self) -> None:
            raise ConnectionError("offline")

        async def set(self, *_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("offline")

        async def execute_command(self, *_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("offline")

    monkeypatch.setattr(runs, "get_redis", OfflineRedis)
    assert (
        client.get(
            f"{URL}/{queued['id']}/stream", headers=normal_user_token_headers
        ).status_code
        == 503
    )
    cancelled = client.post(
        f"{URL}/{queued['id']}/cancel", headers=normal_user_token_headers
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (
        client.post(
            f"{URL}/{queued['id']}/retry", headers=normal_user_token_headers
        ).status_code
        == 503
    )
    detail = client.get(
        f"{URL}/{queued['id']}", headers=normal_user_token_headers
    ).json()
    assert detail["status"] == "failed"
    assert (
        client.get(
            f"{URL}/{queued['id']}/stream", headers=normal_user_token_headers
        ).status_code
        == 200
    )
    version = version_for_user(db)
    response = client.post(
        URL + "/async",
        headers=normal_user_token_headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "offline submit"},
        },
    )
    assert response.status_code == 503
    failed = db.exec(select(Run).where(Run.agent_version_id == version.id)).one()
    assert failed.status == "failed" and failed.finished_at


def test_async_invalid_input_and_sync_cancel_boundary(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
) -> None:
    version = version_for_user(db)
    for message in (None, "", " ", 12, "x" * 20001):
        assert (
            client.post(
                URL + "/async",
                headers=normal_user_token_headers,
                json={
                    "agent_version_id": str(version.id),
                    "input": {"message": message},
                },
            ).status_code
            == 422
        )
    for suffix in ("cancel", "retry"):
        assert (
            client.post(
                f"{URL}/{uuid.uuid4()}/{suffix}", headers=normal_user_token_headers
            ).status_code
            == 404
        )
    queued = submit(client, db, normal_user_token_headers)
    run = db.get(Run, uuid.UUID(queued["id"]))
    assert run is not None
    run.sqlmodel_update({"status": RunStatus.RUNNING, "thread_id": None})
    db.add(run)
    db.commit()
    assert (
        client.post(
            f"{URL}/{run.id}/cancel", headers=normal_user_token_headers
        ).status_code
        == 400
    )
    db.refresh(run)
    assert run.status == "running"
    run.sqlmodel_update({"status": RunStatus.FAILED})
    db.add(run)
    version.snapshot = {"invalid": True}
    db.add(version)
    db.commit()
    assert (
        client.post(
            URL + "/async",
            headers=normal_user_token_headers,
            json={"agent_version_id": str(version.id), "input": {"message": "hi"}},
        ).status_code
        == 400
    )
