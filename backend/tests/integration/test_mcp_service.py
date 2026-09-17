import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.agent.tools.mcp import McpToolExecutor
from app.core.config import settings
from tests.utils.sse import _parse_sse

pytestmark = pytest.mark.mcp_integration


@pytest.mark.skipif(
    os.environ.get("MCP_LIVE_MODEL_TEST") != "1" or not settings.LLM_API_KEY,
    reason="Opt-in real model acceptance",
)
def test_live_model_unavailable_service(
    service: Service, client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    headers = superuser_token_headers
    created = client.post(
        "/api/v1/tools/",
        headers=headers,
        json={
            "name": "unavailable_docs",
            "description": "Read project documentation",
            "tool_type": "mcp",
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
            "timeout_seconds": 5,
        },
    )
    assert created.status_code == 200, created.text
    tool_id = created.json()["id"]
    agent_id = None
    try:
        created = client.post(
            "/api/v1/agents/",
            headers=headers,
            json={
                "name": "MCP outage acceptance",
                "llm_model": settings.LLM_MODEL,
                "tool_ids": [tool_id],
                "timeout_seconds": 60,
                "system_prompt": "Call unavailable_docs once with doc_id 06-async-worker. If it fails, explain the failure in Chinese without retrying or inventing document content.",
            },
        )
        assert created.status_code == 200, created.text
        agent_id = created.json()["id"]
        assert (
            client.post(
                f"/api/v1/agents/{agent_id}/versions", headers=headers, json={}
            ).status_code
            == 200
        )
        conversation = client.post(
            "/api/v1/conversations/", headers=headers, json={"agent_id": agent_id}
        )
        assert conversation.status_code == 200, conversation.text
        service.stop()
        endpoint = f"/api/v1/tools/{tool_id}/test"
        arguments = {"arguments": {"doc_id": "06-async-worker"}}
        failed = client.post(endpoint, headers=headers, json=arguments)
        assert failed.status_code == 200 and not failed.json()["ok"]
        response = client.post(
            f"/api/v1/conversations/{conversation.json()['id']}/stream",
            headers=headers,
            json={"message": "调用工具读取06阶段文档，失败则说明原因，不要重试。"},
        )
        assert response.status_code == 200
        frames = _parse_sse(response.text)
        called = [frame for frame in frames if frame["event"] == "tool_called"]
        results = [frame for frame in frames if frame["event"] == "tool_result"]
        assert len(called) == len(results) == 1, frames
        assert results[0]["data"]["ok"] is False
        finished = next(frame for frame in frames if frame["event"] == "run_finished")
        run_id = finished["data"]["run_id"]
        run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers).json()
        assert run["status"] == "succeeded" and run["output"]["content"]
        assert any(
            e["event_type"] == "tool_result" and e["payload"]["ok"] is False
            for e in events["data"]
        )
        evidence_dir = os.environ.get("MCP_LIVE_EVIDENCE_DIR")
        if evidence_dir:
            path = Path(evidence_dir)
            path.mkdir(parents=True, exist_ok=True)
            (path / "mcp-unavailable-run.json").write_text(
                json.dumps(
                    {"run": run, "events": events["data"]}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
        service.start()
        assert client.post(endpoint, headers=headers, json=arguments).json()["ok"]
    finally:
        if agent_id:
            assert (
                client.delete(f"/api/v1/agents/{agent_id}", headers=headers).status_code
                == 200
            )
        assert (
            client.delete(f"/api/v1/tools/{tool_id}", headers=headers).status_code
            == 200
        )


class Service:
    def __init__(self, root: Path, log: Path, module: str) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.root, self.module = root, module
        self.log: BinaryIO = log.open("wb")
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}
        }
        env.update(
            MCP_DOCS_ROOT=str(self.root),
            MCP_DOCS_PORT=str(self.port),
            PYTHONUNBUFFERED="1",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", self.module],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("MCP test service exited; inspect its captured log")
            try:
                if httpx.get(
                    f"http://127.0.0.1:{self.port}/health", timeout=0.3, trust_env=False
                ).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise TimeoutError("MCP test service did not become healthy")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.fixture
def service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Service]:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "06-async-worker.md").write_text(
        "# Worker\nCheckpoint recovery is a worker responsibility.", encoding="utf-8"
    )
    server = Service(
        root,
        tmp_path / "server.log",
        getattr(request, "param", "app.mcp_server.server"),
    )
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", False)
    monkeypatch.setattr(settings, "MCP_TRUSTED_SERVER_URLS", [server.url])
    try:
        server.start()
        yield server
    finally:
        server.stop()
        server.log.close()


async def test_protocol_and_security(service: Service) -> None:
    async with (
        httpx.AsyncClient(trust_env=False) as http_client,
        streamable_http_client(service.url, http_client=http_client) as (
            read,
            write,
            _,
        ),
    ):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.protocolVersion == "2025-11-25"
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {"search_dev_docs", "read_dev_doc"}
            search = next(tool for tool in tools if tool.name == "search_dev_docs")
            assert search.inputSchema["properties"]["query"]["maxLength"] == 200
            assert search.inputSchema["properties"]["limit"]["maximum"] == 10
            result = await session.call_tool("search_dev_docs", {"query": "Checkpoint"})
            assert not result.isError and "06-async-worker" in result.model_dump_json()
            result = await session.call_tool(
                "read_dev_doc", {"doc_id": "06-async-worker"}
            )
            assert (
                not result.isError
                and "worker responsibility" in result.model_dump_json()
            )
            invalid = await session.call_tool(
                "search_dev_docs", {"query": "x", "limit": 11}
            )
            assert invalid.isError
    async with httpx.AsyncClient(trust_env=False) as client:
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
            response = await client.post(service.url, headers=headers, json={})
            assert response.status_code in {403, 421}


def test_platform_call_restart_and_errors(
    service: Service, client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/v1/tools/",
        headers=superuser_token_headers,
        json={
            "name": "mcp_integration_read",
            "description": "Read shared development docs",
            "tool_type": "mcp",
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
    assert created.status_code == 200, created.text
    tool_id = created.json()["id"]
    endpoint = f"/api/v1/tools/{tool_id}/test"

    def call(arguments: dict[str, str]) -> dict:
        response = client.post(
            endpoint, headers=superuser_token_headers, json={"arguments": arguments}
        )
        assert response.status_code == 200
        return response.json()

    try:
        result = call({"doc_id": "06-async-worker"})
        assert result["ok"] and json.loads(result["content"])["title"] == "Worker"
        assert not call({})["ok"]
        assert not call({"doc_id": "../.env"})["ok"]
        service.stop()
        assert not call({"doc_id": "06-async-worker"})["ok"]
        service.start()
        assert call({"doc_id": "06-async-worker"})["ok"]
        updated = client.patch(
            f"/api/v1/tools/{tool_id}",
            headers=superuser_token_headers,
            json={
                "config": {
                    "transport": "streamable_http",
                    "url": service.url,
                    "tool_name": "unknown",
                }
            },
        )
        assert updated.status_code == 200
        assert not call({"doc_id": "06-async-worker"})["ok"]
    finally:
        assert (
            client.delete(
                f"/api/v1/tools/{tool_id}", headers=superuser_token_headers
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "service", ["tests.integration.mcp_test_server"], indirect=True
)
async def test_timeout_cancel_and_concurrency(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_tasks = asyncio.all_tasks()
    executor = McpToolExecutor()
    config = {
        "transport": "streamable_http",
        "url": service.url,
        "tool_name": "slow_tool",
    }
    timed, other = await asyncio.gather(
        executor.execute(config=config, arguments={"seconds": 2}, timeout_seconds=1),
        executor.execute(config=config, arguments={"seconds": 1.2}, timeout_seconds=3),
    )
    assert not timed.ok and other.ok
    cancelled = asyncio.create_task(
        executor.execute(config=config, arguments={"seconds": 3}, timeout_seconds=5)
    )
    await asyncio.sleep(0.2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert (
        await executor.execute(
            config=config, arguments={"seconds": 0}, timeout_seconds=2
        )
    ).ok
    await executor.aclose()
    assert not executor._connections
    redirect_url = service.url.removesuffix("/mcp") + "/redirect"
    monkeypatch.setattr(
        settings, "MCP_TRUSTED_SERVER_URLS", [service.url, redirect_url]
    )
    redirected = await executor.execute(
        config={**config, "url": redirect_url},
        arguments={"seconds": 0},
        timeout_seconds=2,
    )
    assert not redirected.ok and "ValueError" in str(redirected.error)
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - original_tasks)
