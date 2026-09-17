import asyncio
import ipaddress
import socket
import string
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.agent.tools.base import ToolExecutor, ToolResult
from app.core.config import settings

MAX_RESPONSE_CHARS = 20_000


def _assert_url_allowed(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("Only http and https URLs with a host are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed; use headers")
    if "{" in parsed.netloc or "}" in parsed.netloc:
        raise ValueError("URL parameters are allowed only in the path")
    if settings.ALLOW_PRIVATE_TOOL_URLS:
        return
    if host.lower().rstrip(".") in {"localhost", "metadata.google.internal"}:
        raise ValueError(f"Host '{host}' is not allowed")
    try:
        addresses = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except OSError as exc:
        raise ValueError(f"Cannot resolve host '{host}'") from exc
    if not addresses or any(
        not ipaddress.ip_address(info[4][0]).is_global for info in addresses
    ):
        raise ValueError(f"Host '{host}' resolves to a non-public address")
    # DNS can change between validation and connection (TOCTOU). Pinning the
    # resolved address in a custom transport is outside the phase 05 scope.


def _truncate(content: str, limit: int = MAX_RESPONSE_CHARS) -> str:
    return (
        content
        if len(content) <= limit
        else content[:limit] + f"\n[truncated, total {len(content)} chars]"
    )


class HttpToolExecutor(ToolExecutor):
    def validate_config(self, config: dict[str, Any]) -> None:
        if config.get("method", "GET") not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        }:
            raise ValueError("Unsupported HTTP method")
        url = config.get("url")
        if not isinstance(url, str):
            raise ValueError("HTTP tool requires config.url")
        _assert_url_allowed(url)
        parsed = urlparse(url)
        if "{" in parsed.query or "{" in parsed.fragment:
            raise ValueError("Use query_from_args for query parameters")
        for _, field, spec, conversion in string.Formatter().parse(parsed.path):
            if field is not None and (not field.isidentifier() or spec or conversion):
                raise ValueError("Path placeholders must be plain parameter names")
        headers = config.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("headers must contain string keys and values")
        for name in ("query_from_args", "body_from_args"):
            value = config.get(name, [])
            if not isinstance(value, list) or any(
                not isinstance(v, str) for v in value
            ):
                raise ValueError(f"{name} must be a list of parameter names")

    async def execute(
        self, *, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: int
    ) -> ToolResult:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.to_thread(self.validate_config, config)
                url = config["url"].format_map(
                    {
                        key: quote(str(value), safe="")
                        for key, value in arguments.items()
                    }
                )
                await asyncio.to_thread(_assert_url_allowed, url)
                params = {
                    key: arguments[key]
                    for key in config.get("query_from_args", [])
                    if key in arguments
                }
                body = {
                    key: arguments[key]
                    for key in config.get("body_from_args", [])
                    if key in arguments
                }
                async with httpx.AsyncClient(
                    timeout=timeout_seconds, follow_redirects=False
                ) as client:
                    response = await client.request(
                        config.get("method", "GET"),
                        url,
                        headers=config.get("headers", {}),
                        params=params,
                        json=body or None,
                    )
                content = _truncate(response.text)
                result = ToolResult(
                    ok=response.is_success,
                    content=content,
                    error=None
                    if response.is_success
                    else f"HTTP {response.status_code}: {content}",
                )
        except Exception as exc:
            result = ToolResult(
                ok=False,
                content="",
                error=(str(exc) or f"Timed out after {timeout_seconds}s")[:1000],
            )
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
