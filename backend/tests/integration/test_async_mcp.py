import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlmodel import Session, select

from app.agent.tools.registry import close_tool_executors
from app.core.redis import get_redis
from app.models import RunEvent
from app.services.event_bus import run_stream_key
from app.worker.tasks import execute_run_task
from tests.integration.test_mcp_service import Service, service  # noqa: F401

pytestmark = pytest.mark.mcp_integration


@pytest.mark.parametrize(
    "service", ["tests.integration.mcp_test_server"], indirect=True
)
@pytest.mark.parametrize("mode", ["success", "outage", "timeout", "cancel"])
def test_worker_real_mcp_events_and_cleanup(
    service: Service,  # noqa: F811
    mode: str,
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = superuser_token_headers
    name = f"worker_docs_{uuid.uuid4().hex}"
    slow = mode in {"timeout", "cancel"}
    field = "seconds" if slow else "doc_id"
    args = {"seconds": 2} if slow else {"doc_id": "06-async-worker"}
    tool = client.post(
        "/api/v1/tools/",
        headers=headers,
        json={
            "name": name,
            "description": "Read documents",
            "tool_type": "mcp",
            "config": {
                "transport": "streamable_http",
                "url": service.url,
                "tool_name": "slow_tool" if slow else "read_dev_doc",
            },
            "parameters_schema": {
                "type": "object",
                "properties": {field: {"type": "number" if slow else "string"}},
                "required": [field],
            },
            "timeout_seconds": 1 if mode == "timeout" else 5,
        },
    )
    assert tool.status_code == 200, tool.text
    agent = client.post(
        "/api/v1/agents/",
        headers=headers,
        json={
            "name": name,
            "llm_model": "fake",
            "tool_ids": [tool.json()["id"]],
        },
    ).json()
    version = client.post(
        f"/api/v1/agents/{agent['id']}/versions", headers=headers, json={}
    ).json()
    fake_chat_model.disable_streaming = True
    fake_chat_model.messages = iter(
        [
            AIMessage(
                content="", tool_calls=[{"name": name, "args": args, "id": "mcp"}]
            ),
            AIMessage(content="Tool result received"),
        ]
    )
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **kw: self)
    if mode == "outage":
        service.stop()
    response = client.post(
        "/api/v1/runs/async",
        headers=headers,
        json={
            "agent_version_id": version["id"],
            "input": {"message": "read"},
        },
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert client.portal is not None

    if mode == "cancel":
        future = client.portal.start_task_soon(execute_run_task, {}, run_id)

        async def wait_for_call() -> None:
            async with asyncio.timeout(10):
                while True:
                    events = await get_redis().xrange(run_stream_key(uuid.UUID(run_id)))
                    if any(fields["type"] == "tool_called" for _, fields in events):
                        return
                    await asyncio.sleep(0.02)

        client.portal.call(wait_for_call)
        assert (
            client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code
            == 200
        )
        assert future.result(timeout=10)["status"] == "cancelled"
    else:
        assert client.portal.call(execute_run_task, {}, run_id)["status"] == "succeeded"
    rows = db.exec(select(RunEvent).where(RunEvent.run_id == uuid.UUID(run_id))).all()
    results = [e.payload for e in rows if e.event_type == "tool_result"]
    if mode != "cancel":
        assert len(results) == 1
        assert results[0]["ok"] is (mode == "success")
        if mode == "success":
            assert "worker responsibility" in results[0]["result"]

    async def check_and_close() -> None:
        events = await get_redis().xrange(run_stream_key(uuid.UUID(run_id)))
        assert events[-1][1]["type"] == "run_finished"
        assert sum(fields["type"] == "tool_called" for _, fields in events) == 1
        await close_tool_executors()
        await asyncio.sleep(0)
        pending = [
            t.get_coro().__qualname__
            for t in asyncio.all_tasks()
            if "streamablehttp_client" in t.get_coro().__qualname__
        ]
        assert not pending, pending

    client.portal.call(check_and_close)
