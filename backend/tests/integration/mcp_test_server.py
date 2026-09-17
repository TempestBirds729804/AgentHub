"""Real MCP transport with a delayed tool, exclusively for integration tests."""

import asyncio
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.mcp_server.server import create_server

server = create_server(
    root=Path(os.environ["MCP_DOCS_ROOT"]), port=int(os.environ["MCP_DOCS_PORT"])
)


@server.tool()
async def slow_tool(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "finished"


@server.custom_route("/redirect", methods=["POST", "GET"])
async def redirect(_request: Request) -> RedirectResponse:
    return RedirectResponse("/mcp")


if __name__ == "__main__":
    server.run(transport="streamable-http")
