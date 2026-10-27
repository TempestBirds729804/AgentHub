import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.core import metrics
from app.core.logging import run_id_var
from app.models import Run, RunStatus


@pytest.mark.parametrize(
    "status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED]
)
def test_terminal_metrics_and_nested_gauge(status: RunStatus) -> None:
    run = Run(owner_id=uuid.uuid4(), agent_version_id=uuid.uuid4())
    labels = {"status": status.value, "trigger": "playground"}
    before = REGISTRY.get_sample_value("agenthub_runs_total", labels) or 0
    gauge = REGISTRY.get_sample_value("agenthub_runs_in_progress") or 0
    with metrics.run_observation(run):
        assert run_id_var.get() == str(run.id)
        with metrics.run_observation(run):
            assert REGISTRY.get_sample_value("agenthub_runs_in_progress") == gauge + 1
            run.status = status
            run.duration_ms = 100
    assert REGISTRY.get_sample_value("agenthub_runs_total", labels) == before + 1
    assert REGISTRY.get_sample_value("agenthub_runs_in_progress") == gauge
    assert run_id_var.get() is None


def test_exception_gauge_cleanup() -> None:
    run = Run(owner_id=uuid.uuid4(), agent_version_id=uuid.uuid4())
    before = REGISTRY.get_sample_value("agenthub_runs_in_progress")
    with pytest.raises(RuntimeError), metrics.run_observation(run):
        raise RuntimeError("test")
    assert REGISTRY.get_sample_value("agenthub_runs_in_progress") == before


def test_metrics_endpoint_and_path_cardinality(client: TestClient) -> None:
    for _ in range(3):
        client.get(f"/api/v1/runs/{uuid.uuid4()}")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "agenthub_runs" in response.text
    paths = [
        s.labels.get("path", "")
        for metric in REGISTRY.collect()
        for s in metric.samples
        if s.name == "agenthub_http_requests_total"
    ]
    assert any(p.endswith("/runs/{id}") for p in paths)
    assert all(len(p) < 100 for p in paths)
    assert "/metrics" not in paths


def test_model_label_bounded() -> None:
    assert metrics.model_label(str(uuid.uuid4())) == "other"
