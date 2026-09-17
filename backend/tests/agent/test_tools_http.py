import socket
from typing import Any

import httpx
import pytest

from app.agent.tools.http import HttpToolExecutor, _assert_url_allowed
from app.core.config import settings


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/users/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
def test_blocks_non_public_urls(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", False)
    with pytest.raises(ValueError):
        _assert_url_allowed(url)


def test_public_and_private_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    _assert_url_allowed("https://example.com")
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", True)
    _assert_url_allowed("http://localhost:3001")
    with pytest.raises(ValueError):
        _assert_url_allowed("file:///etc/passwd")


@pytest.mark.parametrize("status", [200, 400, 500, 302])
async def test_http_mapping_and_status(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/repo/a/b"
        assert request.url.params["page"] == "2"
        assert request.headers["x-test"] == "yes"
        return httpx.Response(
            status,
            text="response details",
            headers={"location": "http://localhost/admin"},
        )

    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await HttpToolExecutor().execute(
        config={
            "url": "https://example.com/repo/{owner}/{repo}",
            "headers": {"x-test": "yes"},
            "query_from_args": ["page"],
        },
        arguments={"owner": "a", "repo": "b", "page": 2},
        timeout_seconds=1,
    )
    assert result.ok == (status == 200)
    assert len(requests) == 1
    if status != 200:
        assert "response details" in (result.error or "")


@pytest.mark.parametrize("timeout", [True, False])
async def test_timeout_and_truncation(
    timeout: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_TOOL_URLS", True)

    def handle(_: httpx.Request) -> httpx.Response:
        if timeout:
            raise httpx.ReadTimeout("response timed out")
        return httpx.Response(200, text="a" * 25000)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)),
    )
    result = await HttpToolExecutor().execute(
        config={"url": "http://localhost"}, arguments={}, timeout_seconds=1
    )
    if timeout:
        assert not result.ok and "timed out" in (result.error or "")
    else:
        assert (
            result.ok and "[truncated" in result.content and len(result.content) < 21000
        )
