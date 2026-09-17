import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from app.agent.tools import mcp
from app.agent.tools.http import _assert_url_allowed
from app.core.config import settings


def test_mcp_config(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = mcp.McpToolExecutor()
    for config in (
        {"transport": "stdio", "command": "python", "args": ["-c", "print(1)"]},
        {"transport": "sse", "url": "http://localhost", "tool_name": "test"},
        {"transport": "sse", "url": "https://example.com"},
    ):
        with pytest.raises(ValueError):
            executor.validate_config(config)
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", True)
    executor.validate_config(
        {"transport": "sse", "url": "http://localhost", "tool_name": "test"}
    )


@pytest.mark.parametrize("mode", ["success", "error", "timeout", "connect_error"])
async def test_mcp_cache_and_failures(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", True)
    monkeypatch.setattr(mcp, "SESSION_TTL_SECONDS", 0.03)
    opened = []
    closed = []

    class FakeSession:
        async def call_tool(
            self, name: str, arguments: dict[str, Any]
        ) -> CallToolResult:
            assert name == "test" and arguments == {"q": "hello"}
            if mode == "timeout":
                await asyncio.sleep(2)
            return CallToolResult(
                content=[TextContent(type="text", text="result")],
                isError=mode == "error",
            )

    class FakeClient:
        def __init__(self, _: Any) -> None:
            pass

        @asynccontextmanager
        async def session(self, _: str) -> Any:
            opened.append(True)
            if mode == "connect_error":
                raise ValueError("not connected")
            try:
                yield FakeSession()
            finally:
                closed.append(True)

    monkeypatch.setattr(mcp, "MultiServerMCPClient", FakeClient)
    executor = mcp.McpToolExecutor()
    config = {"transport": "sse", "url": "http://localhost", "tool_name": "test"}
    result = await executor.execute(
        config=config, arguments={"q": "hello"}, timeout_seconds=1
    )
    assert result.ok == (mode == "success")
    if mode == "success":
        again = await executor.execute(
            config=config, arguments={"q": "hello"}, timeout_seconds=1
        )
        assert again.ok and len(opened) == 1
    for connection in executor._connections.values():
        try:
            await asyncio.wait_for(connection.task, 1)
        except asyncio.CancelledError:
            pass
    assert closed or mode == "connect_error"


def test_exact_trusted_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", False)
    monkeypatch.setattr(
        settings, "MCP_TRUSTED_SERVER_URLS", ["http://LOCALHOST:3001/mcp"]
    )
    mcp._assert_mcp_url_allowed("http://localhost:3001/mcp", allow_trusted=True)
    for url in (
        "http://localhost:3001/other",
        "http://localhost:3002/mcp",
        "http://localhost:0/mcp",
        "http://localhost:3001/mcp/",
        "http://localhost:3001/mcp?x=1",
        "http://localhost:3001/mcp#x",
        "http://user@localhost:3001/mcp",
        "http://localhost:3001/a/../mcp",
        "http://localhost:3001/%6dcp",
        "http://169.254.169.254/mcp",
        "http://*.localhost:3001/mcp",
    ):
        with pytest.raises(ValueError):
            mcp._assert_mcp_url_allowed(url, allow_trusted=True)
    with pytest.raises(ValueError):
        _assert_url_allowed("http://localhost:3001/mcp")
    with pytest.raises(ValueError):
        mcp._assert_mcp_url_allowed("http://localhost:3001/mcp")


async def test_request_hook_and_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "MCP_TRUSTED_SERVER_URLS", ["http://localhost:3001/mcp"]
    )
    client = mcp._streamable_http_client()
    try:
        await client.event_hooks["request"][0](
            httpx.Request("POST", "http://localhost:3001/mcp")
        )
        with pytest.raises(ValueError):
            await client.event_hooks["request"][0](
                httpx.Request("POST", "http://localhost:3002/mcp")
            )
        with pytest.raises(ValueError, match="redirects"):
            await client.event_hooks["response"][0](httpx.Response(307))
    finally:
        await client.aclose()


def test_structured_and_empty_results() -> None:
    result = mcp._tool_result(
        CallToolResult(content=[], structuredContent={"data": [1]})
    )
    assert result.ok and result.content == '{"data": [1]}'
    assert mcp._tool_result(CallToolResult(content=[])).content
    assert not mcp._tool_result(CallToolResult(content=[], isError=True)).ok


async def test_shutdown_closes_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", True)
    closed = asyncio.Event()

    class Client:
        def __init__(self, _: Any) -> None:
            pass

        @asynccontextmanager
        async def session(self, _: str) -> Any:
            class Session:
                async def call_tool(self, *_: Any) -> CallToolResult:
                    return CallToolResult(content=[])

            try:
                yield Session()
            finally:
                closed.set()

    monkeypatch.setattr(mcp, "MultiServerMCPClient", Client)
    executor = mcp.McpToolExecutor()
    await executor.execute(
        config={"transport": "sse", "url": "http://localhost", "tool_name": "test"},
        arguments={},
        timeout_seconds=1,
    )
    await executor.aclose()
    await executor.aclose()
    assert closed.is_set() and not executor._connections
