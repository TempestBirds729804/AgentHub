import os
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.mcp_server.documents import load_documents, read_document, search_documents


def create_server(*, root: Path, host: str = "127.0.0.1", port: int = 3001) -> FastMCP:
    documents = load_documents(root)
    hosts = [f"{name}:{port}" for name in ("127.0.0.1", "localhost", "mcp-docs")]
    server = FastMCP(
        "AgentHub development documents",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"http://{name}" for name in hosts],
        ),
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(annotations=annotations, structured_output=False)
    def search_dev_docs(
        query: Annotated[str, Field(min_length=1, max_length=200)],
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> str:
        """Search shared AgentHub development documents; return document IDs, titles and summaries."""
        return search_documents(documents=documents, query=query, limit=limit)

    @server.tool(annotations=annotations, structured_output=False)
    def read_dev_doc(
        doc_id: Annotated[str, Field(min_length=1, max_length=255)],
    ) -> str:
        """Read a shared development document by the exact ID returned by search_dev_docs."""
        return read_document(documents=documents, doc_id=doc_id)

    @server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "documents": len(documents)})

    return server


if __name__ == "__main__":
    create_server(
        root=Path(
            os.environ.get(
                "MCP_DOCS_ROOT", Path(__file__).resolve().parents[3] / "docs/dev"
            )
        ),
        host=os.environ.get("MCP_DOCS_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_DOCS_PORT", "3001")),
    ).run(transport="streamable-http")
