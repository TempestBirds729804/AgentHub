# FastAPI Project - Development

## Local Development

For local development, run PostgreSQL and Mailpit with Docker Compose, and run the FastAPI and Vite development servers locally.

Start the supporting services:

```bash
docker compose up -d db redis minio mailpit
```

Then, from the `backend` directory, install the dependencies and prepare the database:

```bash
uv sync
uv run bash scripts/prestart.sh
```

Start the FastAPI development server:

```bash
uv run fastapi dev
```

In another terminal, from the project root, install the frontend dependencies and start the Vite development server:

```bash
bun install
bun run dev
```

Now you can open these URLs:

Frontend development server: <http://localhost:5173>

Backend API: <http://localhost:8000>

Automatic interactive API documentation with Swagger UI: <http://localhost:8000/docs>

Mailpit: <http://localhost:8025>

The frontend development server uses the backend at `http://localhost:8000`, as configured in `frontend/.env`.

### Frontend Served by FastAPI

Build the frontend from the `frontend` directory:

```bash
bun run build
```

The build is written to `backend/app/frontend` and served by FastAPI at <http://localhost:8000>. Rebuild the frontend after making frontend changes.

## Full Stack with Docker Compose

To run the backend and built frontend in Docker Compose:

```bash
docker compose run --rm backend bash scripts/prestart.sh
docker compose watch
```

Now you can open these URLs:

Application, with the frontend and API served by FastAPI: <http://localhost:8000>

Automatic interactive API documentation with Swagger UI: <http://localhost:8000/docs>

Adminer, database web administration: <http://localhost:8080>

Traefik UI, to see how the routes are being handled by the proxy: <http://localhost:8090>

Mailpit: <http://localhost:8025>

Stop a locally running FastAPI server before starting the Compose backend because both use port `8000`.

**Note**: The first time you start the stack, it might take a minute for all the services to be ready. To monitor it, use `docker compose logs`, or `docker compose logs backend` for the backend service.

## Mailpit

[Mailpit](https://mailpit.axllent.org) captures emails sent during local development instead of delivering them. The local backend connects to it at `localhost:1025`, and the Compose backend connects to the `mailpit` service. Captured emails are available at <http://localhost:8025>.

## Docker Compose Files and Environment Variables

The main `compose.yml` file contains the configuration shared by the whole stack. Docker Compose loads it automatically.

The `compose.override.yml` file adds local development settings, such as mounting the source code as a volume. Docker Compose also loads it automatically and applies it on top of `compose.yml`.

The `compose.deploy.yml` file contains the deployment-specific settings, including HTTPS and automatic certificate handling. It is explicitly combined with `compose.yml` when deploying the application.

The backend reads local settings from the `.env` file. Docker Compose also uses it for variable interpolation and passes the settings each container needs.

Redis supports Agent job queues, caching, rate limiting, and event delivery. It is available locally at `redis://localhost:6379/0`. MinIO provides S3-compatible storage for source documents and run artifacts; its API is available at <http://localhost:9000> and its local console at <http://localhost:9001>. The development credentials and bucket name are configured in `.env`.

On Windows, psycopg async connections require a Selector event loop. From `backend/`, the verified local API command is `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --loop asyncio:SelectorEventLoop`; open the UI at <http://localhost:8000>. The isolated worker compatibility probe and its limitations are recorded in [phase 06](docs/dev/06-async-worker.md#windows-本地-worker-兼容约定). The production worker remains a phase 06 task, after phase 05_1.

### Independent MCP document service

The `mcp-docs` process exposes two read-only tools over Streamable HTTP at `/mcp`. It loads a snapshot of the visible Markdown files directly inside `docs/dev` on startup; restart it after changing those files. It does not read user knowledge bases or application credentials. The local service requires no model key or database.

From `backend/`, run `uv run python -m app.mcp_server.server`. The default host address is `http://127.0.0.1:3001/mcp`; `/health` reports readiness and the document count. `MCP_DOCS_ROOT`, `MCP_DOCS_HOST` and `MCP_DOCS_PORT` override the directory, bind host and port. Host/Origin validation allows only localhost, 127.0.0.1 and mcp-docs at the configured port.

Alternatively, from the repository root run `docker compose up -d --build mcp-docs`. Development Compose publishes only `127.0.0.1:3001`; deployment Compose publishes no port and adds no public route. The container runs as UID 65532 with a read-only filesystem and receives no database, model or S3 credentials.

For an API running on the host, set the following **process environment** before starting the API (PowerShell):

```powershell
$env:MCP_TRUSTED_SERVER_URLS='["http://127.0.0.1:3001/mcp"]'
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --loop asyncio:SelectorEventLoop
```

For a Compose API, the trusted URL is already `http://mcp-docs:3001/mcp`. Use that same URL in the Tool config; a container cannot use host localhost to reach another service. Stage 06 must pass this trust configuration to its worker too. Do not enable `ALLOW_PRIVATE_TOOL_URLS` or stdio. Trust matches an exact scheme/host/port/path, not a hostname prefix or arbitrary internal network. MCP requests use direct HTTP clients, without inheriting machine proxy settings; redirects are rejected. SSE remains supported for existing tools but does not receive this internal endpoint exemption.

In Tools, select MCP and Streamable HTTP. Create the following definitions, choose the URL for your runtime, and bind them to an Agent before publishing:

| Platform name / remote tool name | Required parameters | Optional parameters | Test input |
|---|---|---|---|
| `search_dev_docs` | `query`: string, 1–200 characters | `limit`: integer, 1–10, default 5 | `{"query":"异步执行、状态恢复与限流"}` |
| `read_dev_doc` | `doc_id`: string, 1–255 characters | — | `{"doc_id":"06-async-worker"}` |

The row editor supports parameter names, types, descriptions and required flags. The MCP server enforces the length/range constraints even when they are not expressible in the editor. For API-created tools, the exact schemas below match the service's `tools/list` constraints (display-only schema titles are omitted):

```json
{
  "search_dev_docs": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "minLength": 1, "maxLength": 200},
      "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
    },
    "required": ["query"]
  },
  "read_dev_doc": {
    "type": "object",
    "properties": {"doc_id": {"type": "string", "minLength": 1, "maxLength": 255}},
    "required": ["doc_id"]
  }
}
```

Example config: `{"transport":"streamable_http","url":"http://mcp-docs:3001/mcp","tool_name":"read_dev_doc"}`. Results are JSON text: search returns `{data, count}`, read returns `{doc_id, title, content, truncated}`. Responses remain valid JSON within 16000 characters. Unknown IDs, invalid input and service outages become tool errors. The client does not automatically retry calls; another explicit call reconnects after restart. Returned document content is tool data, not an instruction source.

With a dedicated test database configured, run ordinary tests with `uv run pytest tests/`; real subprocess/network checks are separately required via `uv run pytest -m mcp_integration tests/integration -v`. These three integration tests require no LLM key and launch/stop their own local servers. Linux must also run `tests/mcp_server` because Windows accounts without symlink privileges skip two link-isolation cases. The real-model Playwright test in `tests/tools.spec.ts` additionally needs `LLM_API_KEY`, `MCP_SERVICE_URL`, `PLAYWRIGHT_BASE_URL`, and a working API; its name contains `independent MCP service`.

Set `MCP_LIVE_MODEL_TEST=1` with valid model credentials to also run the fourth backend integration test: a real model handles an unavailable, test-owned MCP process and a new tool call succeeds after that process restarts. Optional `MCP_LIVE_EVIDENCE_DIR` saves its Run/Event JSON before test cleanup. The ordinary integration command skips only this opt-in model test; it still runs all three real MCP network tests. See [phase 05_1 acceptance](docs/dev/05_1-mcp-service.md#偏差记录) for actual results and browser evidence.

After changing variables, make sure you restart the stack:

```bash
docker compose watch
```

## Async runs and workers (phase 06)

Run `uv run bash scripts/prestart.sh` in `backend/` before starting the API and worker. It migrates business tables, creates LangGraph's four checkpoint tables idempotently, and initializes users. On Windows without Bash, run these commands in order:

```powershell
uv run python app/backend_pre_start.py
uv run alembic upgrade head
uv run python app/setup_checkpointer.py
uv run python app/initial_data.py
```

Start the Compose worker with `docker compose up -d worker`, then inspect `docker compose logs -f worker`. For development, `docker compose watch worker` synchronizes source and restarts the process. arq 0.28's built-in `--watch` does not reimport application modules; this project uses Compose `sync+restart`. A native Windows worker uses `uv run python -m app.worker.main` from `backend/`; press Ctrl+C and start it again after changing code. Its Selector event loop supports psycopg and waits for resource cleanup on exit.

Use **Submit async** on an Agent version for queued work. Run details replay buffered events, follow output, and offer Cancel, Retry from checkpoint, or Run again from scratch. The list refreshes active runs every three seconds. **Quick run** remains the blocking API; Playground retains its own streaming and cancellation flow. Only asynchronous work uses the details-page cancel and GET stream endpoints.

`POST /api/v1/runs/async` returns 202; `POST /runs/{id}/cancel` requests cancellation at a graph node boundary; `POST /runs/{id}/retry?fresh=false` reuses the latest checkpoint and `fresh=true` starts a new thread. Retry is restricted to failed/cancelled Runs, at most three times. A still-stopping execution returns 409. GET `/api/v1/runs/{id}/stream` requires the same Bearer authentication as other API requests and replays retained Redis events from the beginning. Durable non-token events remain in PostgreSQL; Redis retains approximately 1000 events per Run for one hour.

The default per-user limit is three queued/running async Runs (`AGENT_MAX_CONCURRENT_RUNS_PER_USER`); excess submissions return 429. Agent timeout must be at most 900 seconds. Queue failure returns 503 and persists a failed Run. arq retries interrupted jobs up to `max_tries=2`; ordinary application errors are saved as failed and require explicit retry. A five-minute sweep fails interrupted running records older than 1200 seconds. Redis slot markers make release idempotent and have a one-hour expiry.

Each Run uses a separate checkpoint thread, including in conversations; message history comes from the business message table. Recovery reloads MCP clients from current tool configuration and never serializes connections. Completed checkpointed nodes are skipped, but a tool call interrupted before its checkpoint is committed can run again. Current crash acceptance uses read-only MCP tools; side-effecting tools must provide their own idempotency.

Use a dedicated database and Redis DB 15 for tests; tests delete business records during teardown. Run `uv run pytest tests/`, and separately `uv run pytest -m mcp_integration tests/integration -v`. The latter includes real MCP success, outage, timeout, cancellation, and client-cleanup checks. Coverage is configured for both threads and SQLAlchemy greenlets. With the development API, model credentials and worker running, `ASYNC_LIVE_TEST=1` enables `frontend/tests/runs-async.spec.ts`. `uv run python scripts/check_async_worker.py` runs the disruptive manual acceptance (kill worker, stop Redis, concurrency limit); it first rejects unrelated active Runs, restores services in finally blocks, and keeps its own Run/Agent/Tool records plus evidence in `frontend/test-results/phase06-manual/` for review.

## The `.env` File

The tracked `.env` file contains local development defaults, passwords, and other configuration. Its hostnames use `localhost` for processes running on your machine. Docker Compose overrides hostnames such as the database and SMTP server with their Compose service names.

Do not store deployment secrets in `.env`. Configure them as described in the [FastAPI Cloud deployment guide](./deployment.md) or the [Docker Compose deployment guide](./deployment-docker-compose.md).

## Pre-commit Hooks and Code Linting

The project uses [prek](https://prek.j178.dev/), a modern alternative to [pre-commit](https://pre-commit.com/), for code linting and formatting.

You can find a file `.pre-commit-config.yaml` with configurations at the root of the project.

### Install `prek` to Run Automatically

`prek` is already part of the dependencies of the project.

From the project root, install the Git hook so that `prek` runs automatically before each commit:

```bash
uv run prek install -f
```

The `-f` flag forces the installation, in case there was already a `pre-commit` hook previously installed.

Now whenever you try to commit, for example with:

```bash
git commit
```

`prek` will check and format the code you are about to commit. If it modifies any files, add those files to Git again before committing.

### Run `prek` Manually

You can also run `prek` manually on all files from the project root:

```bash
uv run prek run --all-files
```
