import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from mcp import ClientSession

from app.worker.tasks import execute_run_task, resume_run_task
from tests.integration.test_mcp_service import Service, service  # noqa: F401

pytestmark = pytest.mark.mcp_integration


@pytest.mark.parametrize("approved", [True, False])
def test_real_mcp_approval_gate(
    service: Service,  # noqa: F811
    client: TestClient,
    superuser_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
) -> None:
    headers = superuser_token_headers
    name = f"approval_docs_{uuid.uuid4().hex}"
    result = client.post(
        "/api/v1/tools/",
        headers=headers,
        json={
            "name": name,
            "description": "Read development docs",
            "tool_type": "mcp",
            "requires_approval": True,
            "config": {
                "transport": "streamable_http",
                "url": service.url,
                "tool_name": "read_dev_doc",
            },
            "parameters_schema": {
                "type": "object",
                "properties": {"doc_id": {"type": "string"}},
                "required": ["doc_id"],
            },
        },
    )
    assert result.status_code == 200, result.text
    agent = client.post(
        "/api/v1/agents/",
        headers=headers,
        json={"name": name, "llm_model": "fake", "tool_ids": [result.json()["id"]]},
    ).json()
    version = client.post(
        f"/api/v1/agents/{agent['id']}/versions", headers=headers, json={}
    ).json()
    fake_chat_model.disable_streaming = True
    fake_chat_model.messages = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "mcp-approval",
                        "name": name,
                        "args": {"doc_id": "06-async-worker"},
                    }
                ],
            ),
            AIMessage(content="finished"),
        ]
    )
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **kw: self)
    original = ClientSession.call_tool
    calls: list[str] = []

    async def observed(
        self: ClientSession, name: str, *args: Any, **kwargs: Any
    ) -> Any:
        calls.append(name)
        return await original(self, name, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "call_tool", observed)
    result = client.post(
        "/api/v1/runs/async",
        headers=headers,
        json={"agent_version_id": version["id"], "input": {"message": "read"}},
    )
    assert result.status_code == 202
    run_id = result.json()["id"]
    assert client.portal is not None
    assert (
        client.portal.call(execute_run_task, {}, run_id)["status"] == "waiting_approval"
    )
    assert calls == []
    request = client.get(
        "/api/v1/approvals/", headers=headers, params={"run_id": run_id}
    ).json()["data"][0]
    assert (
        client.post(
            f"/api/v1/approvals/{request['id']}/decide",
            headers=headers,
            json={"approved": approved, "rejection_reason": "Do not read documents"},
        ).status_code
        == 200
    )
    assert calls == []
    assert client.portal.call(resume_run_task, {}, run_id)["status"] == "succeeded"
    assert calls == (["read_dev_doc"] if approved else [])
    events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers).json()["data"]
    results = [e["payload"] for e in events if e["event_type"] == "tool_result"]
    if approved:
        assert len(results) == 1 and results[0]["ok"]
        assert "worker responsibility" in results[0]["result"]
    else:
        assert not results
