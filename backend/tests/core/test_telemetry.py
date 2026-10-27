import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.core import telemetry
from app.core.config import settings
from app.core.metrics import run_observation
from app.models import Run, RunStatus


def test_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setattr(telemetry, "_provider", None)
    telemetry.setup_telemetry(service_name="test")
    assert telemetry._provider is None


@pytest.mark.asyncio
async def test_span_hierarchy_attributes_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_provider", provider)

    @telemetry.traced("agent.node.test")
    async def node() -> None:
        raise ValueError("secret content must not be exported")

    run = Run(owner_id=uuid.uuid4(), agent_version_id=uuid.uuid4())
    with run_observation(run):
        with pytest.raises(ValueError):
            await node()
        run.status = RunStatus.FAILED
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["agent.node.test", "agent.run"]
    assert spans[0].parent.span_id == spans[1].context.span_id
    assert all(s.status.status_code == StatusCode.ERROR for s in spans)
    for span in spans:
        assert not span.events
        assert all(
            k.startswith("agenthub.") and len(str(v)) < 500
            for k, v in span.attributes.items()
        )
    provider.shutdown()
