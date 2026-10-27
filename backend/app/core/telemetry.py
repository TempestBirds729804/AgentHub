from collections.abc import Awaitable, Callable
from functools import wraps

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import settings

_provider: TracerProvider | None = None


def setup_telemetry(*, service_name: str, app: FastAPI | None = None) -> None:
    global _provider
    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT or _provider is not None:
        return
    from app.core.async_db import async_engine
    from app.core.db import engine

    _provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": settings.PROJECT_VERSION,
                "deployment.environment": settings.FASTAPI_ENV or "production",
            }
        )
    )
    _provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
                insecure=settings.OTEL_EXPORTER_OTLP_ENDPOINT.startswith("http://"),
            )
        )
    )
    if app is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=_provider,
            excluded_urls="/metrics,/api/v1/utils/health-check/",
        )
    SQLAlchemyInstrumentor().instrument(
        engines=[engine, async_engine.sync_engine], tracer_provider=_provider
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=_provider)


def shutdown_telemetry(*, app: FastAPI | None = None) -> None:
    global _provider
    if _provider is not None:
        if app is not None:
            FastAPIInstrumentor.uninstrument_app(app)
        SQLAlchemyInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()
        _provider.shutdown()
        _provider = None


def get_tracer() -> trace.Tracer:
    return (_provider or trace.NoOpTracerProvider()).get_tracer("agenthub")


def traced[**P, R](
    name: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with get_tracer().start_as_current_span(
                name, record_exception=False, set_status_on_exception=False
            ) as span:
                state = args[0] if args else kwargs.get("state")
                if isinstance(state, dict):
                    iteration = state.get("iteration")
                    if isinstance(iteration, int):
                        span.set_attribute("agenthub.iteration", iteration)
                    model = state.get("llm_model")
                    if isinstance(model, str):
                        span.set_attribute("agenthub.llm_model", model[:128])
                tools = kwargs.get("tools")
                if isinstance(tools, list):
                    span.set_attribute("agenthub.tool_count", len(tools))
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    span.set_attribute("agenthub.error_type", type(exc).__name__[:128])
                    span.set_status(trace.StatusCode.ERROR)
                    raise

        return wrapped

    return decorate
