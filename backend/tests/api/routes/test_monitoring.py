import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api.routes import admin_monitoring as monitoring
from app.models import RunStatus
from tests.utils.run import create_random_run


@pytest.mark.parametrize(
    "path", ["overview", "usage", "top-users", "recent-failures", "system"]
)
def test_admin_only(
    client: TestClient, normal_user_token_headers: dict[str, str], path: str
) -> None:
    assert (
        client.get(
            f"/api/v1/monitoring/{path}", headers=normal_user_token_headers
        ).status_code
        == 403
    )
    assert client.get(f"/api/v1/monitoring/{path}").status_code == 401


def test_aggregates(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    def overview() -> dict:
        response = client.get(
            "/api/v1/monitoring/overview", headers=superuser_token_headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    before = overview()
    created = []
    for i in range(3):
        run = create_random_run(db)
        run.status = RunStatus.FAILED if i == 2 else RunStatus.SUCCEEDED
        run.duration_ms = 100
        run.prompt_tokens = 10
        run.completion_tokens = 5
        run.cost_usd = Decimal(".01")
        run.error = "example failure" if i == 2 else None
        db.add(run)
        db.commit()
        db.refresh(run)
        created.append(run)
    after = overview()
    assert after["total_runs"] == before["total_runs"] + 3
    assert after["runs_24h"] == before["runs_24h"] + 3
    previous_successes = (before["success_rate_24h"] or 0) * before["runs_24h"]
    assert after["success_rate_24h"] == pytest.approx(
        (previous_successes + 2) / after["runs_24h"]
    )
    assert after["total_tokens_30d"] == before["total_tokens_30d"] + 45
    response = client.get(
        "/api/v1/monitoring/usage?days=7", headers=superuser_token_headers
    )
    assert response.status_code == 200, response.text
    usage = response.json()["data"]
    assert len(usage) == 7 and usage == sorted(usage, key=lambda d: d["day"])
    assert usage[-1]["runs"] >= 3
    assert (
        client.get(
            "/api/v1/monitoring/usage?days=100", headers=superuser_token_headers
        ).status_code
        == 422
    )
    top = client.get("/api/v1/monitoring/top-users", headers=superuser_token_headers)
    assert top.status_code == 200, top.text
    failures = client.get(
        "/api/v1/monitoring/recent-failures", headers=superuser_token_headers
    ).json()["data"]
    assert any(r["id"] == str(created[-1].id) for r in failures)
    assert all(r["id"] not in [str(v.id) for v in created[:2]] for r in failures)
    assert failures == sorted(failures, key=lambda r: r["created_at"], reverse=True)
    created[-1].cost_usd = None
    db.add(created[-1])
    db.commit()
    assert overview()["total_cost_usd_30d"] is None


def test_system_redis_timeout(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*_args: object) -> None:
        await asyncio.sleep(10)

    redis = AsyncMock()
    redis.ping.side_effect = redis.exists.side_effect = unavailable
    monkeypatch.setattr(monitoring, "get_redis", lambda: redis)
    start = time.perf_counter()
    response = client.get("/api/v1/monitoring/system", headers=superuser_token_headers)
    assert response.status_code == 200, response.text
    assert time.perf_counter() - start < 5
    data = response.json()
    assert data["queue_depth"] is None
    components = {c["name"]: c for c in data["components"]}
    assert (
        not components["Redis"]["healthy"]
        and components["Redis"]["detail"] == "Timeout"
    )
    assert components["Postgres"]["healthy"]
    assert (
        client.get(
            "/api/v1/monitoring/overview", headers=superuser_token_headers
        ).status_code
        == 200
    )
