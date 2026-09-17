# AgentHub

Function 工具仅执行平台内置白名单函数（calculator、current_time），不执行用户提交的 Python 代码。外部能力支持 HTTP、MCP Streamable HTTP 和旧 SSE 工具。独立 `mcp-docs` 服务提供开发文档搜索与读取，启动和配置见 [开发说明](development.md#independent-mcp-document-service)。HTTP/MCP 默认禁止访问内网，仅部署方指定的 MCP 端点可定向放行；MCP stdio 禁用，`requires_approval` 的实际拦截在阶段 07 实现。

AgentHub is an AI Agent management and execution platform built on FastAPI and React. It keeps the template's authentication and administration capabilities while adding a stateful LangGraph runtime, versioned Agent definitions, execution tracing, tools, approval workflows, and knowledge retrieval.

## Capabilities

- Manage Agent definitions and immutable published versions.
- Run stateful Agents through LangGraph with synchronous streaming and asynchronous workers.
- Track conversations, runs, events, token usage, latency, and cost.
- Connect function, HTTP, and MCP tools with optional human approval.
- Build pgvector-backed knowledge bases from documents stored in S3-compatible storage.
- Compare Agent versions with repeatable evaluation cases.

## Technology Stack

- FastAPI, SQLModel, PostgreSQL, and pgvector
- LangGraph and an OpenAI-compatible model adapter
- Redis and arq for asynchronous execution
- MinIO for S3-compatible object storage
- React, TypeScript, TanStack Router, TanStack Query, Tailwind CSS, and shadcn/ui
- Pytest and Playwright for automated testing
- Docker Compose and Traefik for local and self-hosted environments

## Quick Start

1. Review the local defaults in `.env`. Keep `LLM_API_KEY` empty for CRUD-only development, or set it locally when model execution is required. Never commit a real API key.
2. Start the infrastructure services:

   ```bash
   docker compose up -d db redis minio mailpit
   ```

3. Install and initialize the backend:

   ```bash
   cd backend
   uv sync
   uv run bash scripts/prestart.sh
   uv run fastapi dev
   ```

4. In another terminal, start the frontend:

   ```bash
   bun install
   bun run dev
   ```

The frontend is available at <http://localhost:5173>, the API documentation at <http://localhost:8000/docs>, the MinIO console at <http://localhost:9001>, and Mailpit at <http://localhost:8025>.

The implementation roadmap and phase specifications are in [`docs/dev/`](./docs/dev/). General local-development guidance is in [`development.md`](./development.md), backend-specific guidance is in [`backend/README.md`](./backend/README.md), and deployment guidance is in [`deployment.md`](./deployment.md) and [`deployment-docker-compose.md`](./deployment-docker-compose.md).

## License

AgentHub is licensed under the terms of the MIT license.
