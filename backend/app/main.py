from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI, Response
from fastapi.routing import APIRoute
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.middleware.cors import CORSMiddleware

from app.agent.tools.registry import close_tool_executors
from app.api.main import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.metrics import MetricsMiddleware, metrics_payload
from app.core.redis import close_redis
from app.core.telemetry import setup_telemetry, shutdown_telemetry
from app.worker.settings import close_arq_pool

FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    try:
        yield
    finally:
        await close_tool_executors()
        await close_arq_pool()
        await close_redis()
        shutdown_telemetry(app=_app)


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_HOST],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(MetricsMiddleware)
setup_telemetry(service_name="agenthub-api", app=app)


@app.get("/metrics", include_in_schema=False, tags=["metrics"])
async def metrics_endpoint() -> Response:
    """Internal scrape endpoint; Traefik explicitly excludes this path."""
    return Response(await metrics_payload(), media_type=CONTENT_TYPE_LATEST)


app.include_router(api_router, prefix=settings.API_V1_STR)
app.frontend("/", directory=FRONTEND_DIR)
