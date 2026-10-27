import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from opentelemetry.trace import StatusCode
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import run_id_var, user_id_var
from app.core.telemetry import get_tracer
from app.models import Run

http_requests_total = Counter(
    "agenthub_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
http_request_duration_seconds = Histogram(
    "agenthub_http_request_duration_seconds", "HTTP duration", ["method", "path"]
)
runs_total = Counter(
    "agenthub_runs_total", "Terminal run outcomes", ["status", "trigger"]
)
run_duration_seconds = Histogram(
    "agenthub_run_duration_seconds",
    "Run duration",
    ["trigger"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
runs_in_progress = Gauge(
    "agenthub_runs_in_progress", "Executing runs", multiprocess_mode="livesum"
)
llm_tokens_total = Counter(
    "agenthub_llm_tokens_total", "Model tokens", ["model", "kind"]
)
llm_cost_usd_total = Counter(
    "agenthub_llm_cost_usd_total", "Estimated model cost (known prices only)", ["model"]
)
llm_call_duration_seconds = Histogram(
    "agenthub_llm_call_duration_seconds",
    "Model latency",
    ["model"],
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
llm_errors_total = Counter(
    "agenthub_llm_errors_total", "Model errors", ["model", "error_type"]
)
tool_calls_total = Counter(
    "agenthub_tool_calls_total", "Tool calls", ["tool_type", "outcome"]
)
tool_duration_seconds = Histogram(
    "agenthub_tool_duration_seconds", "Tool duration", ["tool_type"]
)
retrieval_duration_seconds = Histogram(
    "agenthub_retrieval_duration_seconds", "Retrieval duration"
)
retrieval_results_count = Histogram(
    "agenthub_retrieval_results", "Retrieved chunk count", buckets=(0, 1, 2, 3, 5, 10)
)
approvals_total = Counter(
    "agenthub_approvals_total", "Approval decisions", ["decision"]
)
queue_depth = Gauge(
    "agenthub_queue_depth",
    "Pending jobs; -1 when unavailable",
    multiprocess_mode="livemostrecent",
)
evals_total = Counter("agenthub_evals_total", "Evaluation batch outcomes", ["status"])
_run_depth: ContextVar[int] = ContextVar("metrics_run_depth", default=0)


def model_label(model: str) -> str:
    from app.agent.pricing import MODEL_PRICING

    return (
        model
        if model in {*MODEL_PRICING, settings.LLM_MODEL, settings.EVAL_JUDGE_MODEL}
        else "other"
    )


def metrics_registry() -> CollectorRegistry:
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not path:
        return REGISTRY
    # Uvicorn has no gunicorn child_exit hook. Reap dead live gauges on scrape.
    # Only the configured process-private tmpfs is inspected; the library owns deletion.
    for filename in os.listdir(path):
        match = re.fullmatch(r"gauge_live\w+_(\d+)\.db", filename)
        if match:
            pid = int(match[1])
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                multiprocess.mark_process_dead(pid, path=path)  # type: ignore[no-untyped-call]
            except PermissionError:
                pass
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return registry


async def update_queue_depth() -> int | None:
    from app.core.redis import get_redis

    try:
        async with asyncio.timeout(2):
            depth = int(await get_redis().zcard("arq:queue"))
        queue_depth.set(depth)
        return depth
    except Exception:
        queue_depth.set(-1)
        return None


async def metrics_payload() -> bytes:
    await update_queue_depth()
    return generate_latest(metrics_registry())


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in (
            "/metrics",
            "/api/v1/utils/health-check/",
        ):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def observe(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            path = getattr(scope.get("route"), "path", "unknown")
            method = (
                scope["method"]
                if scope["method"]
                in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
                else "other"
            )
            http_requests_total.labels(
                method=method, path=path, status=str(status)
            ).inc()
            http_request_duration_seconds.labels(method=method, path=path).observe(
                time.perf_counter() - started
            )


@contextmanager
def run_observation(run: Run) -> Iterator[None]:
    depth = _run_depth.set(_run_depth.get() + 1)
    if _run_depth.get() > 1:
        try:
            yield
        finally:
            _run_depth.reset(depth)
        return
    run_token, user_token = (
        run_id_var.set(str(run.id)),
        user_id_var.set(str(run.owner_id)),
    )
    initial_status = getattr(run.status, "value", run.status)
    trigger = str(getattr(run.trigger, "value", run.trigger))
    runs_in_progress.inc()
    logging.getLogger(__name__).info("Run execution started")
    with get_tracer().start_as_current_span(
        "agent.run", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("agenthub.run_id", str(run.id))
        span.set_attribute("agenthub.agent_version_id", str(run.agent_version_id))
        span.set_attribute("agenthub.trigger", trigger)
        try:
            yield
        finally:
            # Failed transactions may expire ORM attributes; observability must
            # never trigger lazy I/O or mask the original application error.
            values = run.__dict__
            status = str(
                getattr(values.get("status"), "value", values.get("status", "unknown"))
            )
            span.set_attribute("agenthub.status", status)
            if status == "failed":
                span.set_status(StatusCode.ERROR)
            span.set_attribute("agenthub.prompt_tokens", values.get("prompt_tokens", 0))
            span.set_attribute(
                "agenthub.completion_tokens", values.get("completion_tokens", 0)
            )
            if status in (
                "succeeded",
                "failed",
                "cancelled",
            ) and initial_status not in ("succeeded", "failed", "cancelled"):
                runs_total.labels(status=status, trigger=trigger).inc()
                if values.get("duration_ms") is not None:
                    run_duration_seconds.labels(trigger=trigger).observe(
                        values["duration_ms"] / 1000
                    )
            runs_in_progress.dec()
            logging.getLogger(__name__).info("Run execution finished: %s", status)
            run_id_var.reset(run_token)
            user_id_var.reset(user_token)
            _run_depth.reset(depth)


def observe_run[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        run = kwargs["run"]
        assert isinstance(run, Run)
        with run_observation(run):
            return await fn(*args, **kwargs)

    return wrapped


def observe_stream[**P, R](
    fn: Callable[P, AsyncGenerator[R]],
) -> Callable[P, AsyncGenerator[R]]:
    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[R]:
        run = kwargs["run"]
        assert isinstance(run, Run)
        with run_observation(run):
            stream = fn(*args, **kwargs)
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()

    return wrapped
