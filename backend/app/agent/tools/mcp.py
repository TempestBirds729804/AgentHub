import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.types import CallToolResult

from app.agent.tools.base import ToolExecutor, ToolResult
from app.agent.tools.http import _assert_url_allowed, _truncate
from app.core.config import settings

SESSION_TTL_SECONDS = 60


def _endpoint(url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(c.isspace() or ord(c) < 32 or c in "\\%*?#{}" for c in url)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError(
            "MCP URL must be an exact HTTP endpoint without credentials, query or fragment"
        )
    port = (
        parsed.port
        if parsed.port is not None
        else (443 if parsed.scheme == "https" else 80)
    )
    if port == 0:
        raise ValueError("MCP URL port must be between 1 and 65535")
    return (
        parsed.scheme,
        parsed.hostname.lower(),
        port,
        parsed.path or "/",
    )


def _assert_mcp_url_allowed(url: str, *, allow_trusted: bool = False) -> None:
    if not allow_trusted:
        # Legacy SSE servers supply a session_id query on their message endpoint.
        _assert_url_allowed(url)
        return
    endpoint = _endpoint(url)
    if allow_trusted and endpoint in {
        _endpoint(value) for value in settings.MCP_TRUSTED_SERVER_URLS
    }:
        return
    _assert_url_allowed(url)


async def _reject_redirect(response: httpx.Response) -> None:
    if response.is_redirect:
        raise ValueError("MCP redirects are not allowed")


def _http_client(
    headers: dict[str, Any] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    *,
    allow_trusted: bool = False,
) -> httpx.AsyncClient:
    async def check_request(request: httpx.Request) -> None:
        await asyncio.to_thread(
            _assert_mcp_url_allowed, str(request.url), allow_trusted=allow_trusted
        )

    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30),
        auth=auth,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [check_request], "response": [_reject_redirect]},
    )


def _streamable_http_client(
    headers: dict[str, Any] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return _http_client(headers, timeout, auth, allow_trusted=True)


def _tool_result(response: CallToolResult) -> ToolResult:
    content = (
        json.dumps(response.structuredContent, ensure_ascii=False)
        if response.structuredContent is not None
        else "\n".join(
            block.text if block.type == "text" else block.model_dump_json()
            for block in response.content
        )
    )
    content = _truncate(
        content
        or (
            "MCP tool returned an error"
            if response.isError
            else "MCP tool returned no content"
        )
    )
    return ToolResult(
        ok=not response.isError,
        content=content,
        error=content if response.isError else None,
    )


def _request_error(exc: Exception) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return _request_error(
            next(e for e in exc.exceptions if isinstance(e, Exception))
        )
    if isinstance(exc, httpx.TimeoutException):
        return "MCP connection timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"MCP server returned HTTP {exc.response.status_code}"
    return f"MCP request failed ({type(exc).__name__})"


@dataclass
class _Call:
    name: str
    arguments: dict[str, Any]
    result: asyncio.Future[ToolResult]


class _Connection:
    def __init__(self, url: str) -> None:
        self.queue: asyncio.Queue[_Call] = asyncio.Queue()
        self.task = asyncio.create_task(self._serve(url))

    async def _serve(self, url: str) -> None:
        """Own the session's enter/exit in one task, including its AnyIO scopes."""
        current: _Call | None = None
        try:
            client = MultiServerMCPClient(
                {
                    "tool": {
                        "transport": "sse",
                        "url": url,
                        "httpx_client_factory": _http_client,
                    }
                }
            )
            async with client.session("tool") as session:
                while True:
                    try:
                        current = await asyncio.wait_for(
                            self.queue.get(), SESSION_TTL_SECONDS
                        )
                    except TimeoutError:
                        return
                    if current.result.cancelled():
                        continue
                    response = await session.call_tool(current.name, current.arguments)
                    if not current.result.done():
                        current.result.set_result(_tool_result(response))
                    current = None
        except Exception as exc:
            if current is not None and not current.result.done():
                current.result.set_result(
                    ToolResult(ok=False, content="", error=_request_error(exc))
                )
        finally:
            if current is not None and not current.result.done():
                current.result.set_result(
                    ToolResult(ok=False, content="", error="MCP connection closed")
                )
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if not pending.result.done():
                    pending.result.set_result(
                        ToolResult(ok=False, content="", error="MCP connection closed")
                    )


class McpToolExecutor(ToolExecutor):
    def __init__(self) -> None:
        self._connections: dict[
            tuple[asyncio.AbstractEventLoop, str, str], _Connection
        ] = {}

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        connections = [
            self._connections.pop(key)
            for key in list(self._connections)
            if key[0] is loop
        ]
        for connection in connections:
            connection.task.cancel()
        await asyncio.gather(*(c.task for c in connections), return_exceptions=True)

    def validate_config(self, config: dict[str, Any]) -> None:
        if config.get("transport") not in {"sse", "streamable_http"}:
            raise ValueError(
                "Only SSE and Streamable HTTP are enabled; stdio MCP tools are disabled"
            )
        if (
            not isinstance(config.get("tool_name"), str)
            or not config["tool_name"].strip()
        ):
            raise ValueError("MCP tool requires config.tool_name")
        if not isinstance(config.get("url"), str):
            raise ValueError("MCP tool requires config.url")
        _assert_mcp_url_allowed(
            config["url"], allow_trusted=config["transport"] == "streamable_http"
        )

    async def execute(
        self, *, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: int
    ) -> ToolResult:
        started = time.perf_counter()
        connection: _Connection | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.to_thread(self.validate_config, config)
                if config["transport"] == "streamable_http":
                    client = MultiServerMCPClient(
                        {
                            "tool": {
                                "transport": "streamable_http",
                                "url": config["url"],
                                "httpx_client_factory": _streamable_http_client,
                            }
                        }
                    )
                    async with client.session("tool") as session:
                        result = _tool_result(
                            await session.call_tool(config["tool_name"], arguments)
                        )
                    result.duration_ms = int((time.perf_counter() - started) * 1000)
                    return result
                loop = asyncio.get_running_loop()
                key = (loop, config["transport"], config["url"])
                for expired in [
                    k for k, v in self._connections.items() if v.task.done()
                ]:
                    del self._connections[expired]
                connection = self._connections.get(key)
                if connection is None:
                    connection = _Connection(config["url"])
                    self._connections[key] = connection
                future: asyncio.Future[ToolResult] = loop.create_future()
                connection.queue.put_nowait(
                    _Call(config["tool_name"], arguments, future)
                )
                result = await future
        except (TimeoutError, asyncio.CancelledError) as exc:
            if connection is not None:
                connection.task.cancel()
                with suppress(asyncio.CancelledError):
                    await connection.task
            if isinstance(exc, asyncio.CancelledError):
                raise
            result = ToolResult(
                ok=False, content="", error=f"Timed out after {timeout_seconds}s"
            )
        except Exception as exc:
            result = ToolResult(ok=False, content="", error=_request_error(exc))
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
