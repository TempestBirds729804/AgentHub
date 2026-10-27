import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.routes import evals
from app.models import EvalResult, EvalRun
from tests.utils.agent import create_random_agent, create_random_agent_version
from tests.utils.eval import create_eval_batch


def owned_agent(client: TestClient, db: Session, headers: dict[str, str]) -> Any:
    user_id = uuid.UUID(client.get("/api/v1/users/me", headers=headers).json()["id"])
    return create_random_agent(db, owner_id=user_id)


def test_case_crud_import_and_validation(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    headers = normal_user_token_headers
    agent = owned_agent(client, db, headers)
    url = f"/api/v1/evals/cases?agent_id={agent.id}"
    body = {
        "name": "case",
        "input": {"message": "hello"},
        "assertions": [{"type": "contains", "value": "hello"}],
    }
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 200
    case_id = response.json()["id"]
    assert (
        client.get(f"/api/v1/evals/cases/{case_id}", headers=headers).json()["name"]
        == "case"
    )
    assert (
        client.patch(
            f"/api/v1/evals/cases/{case_id}", headers=headers, json={"name": "renamed"}
        ).json()["name"]
        == "renamed"
    )
    assert client.get(url, headers=headers).json()["count"] == 1
    for assertion in [
        {"type": "invalid", "value": "x"},
        {"type": "regex", "value": "["},
        {"type": "llm_judge", "value": "good", "threshold": 2},
        {"type": "max_cost_usd", "value": "NaN"},
    ]:
        response = client.post(
            url, headers=headers, json={**body, "assertions": [assertion]}
        )
        assert response.status_code == 400
    assert (
        client.patch(
            f"/api/v1/evals/cases/{case_id}", headers=headers, json={"input": None}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/v1/evals/cases/import?agent_id={agent.id}",
            headers=headers,
            json=[body] * 5,
        ).json()["count"]
        == 5
    )
    assert (
        client.delete(f"/api/v1/evals/cases/{case_id}", headers=headers).status_code
        == 200
    )
    foreign = create_random_agent(db)
    assert (
        client.get(
            f"/api/v1/evals/cases?agent_id={foreign.id}", headers=headers
        ).status_code
        == 403
    )


def test_enqueue_selection_failure_and_cancel(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = normal_user_token_headers
    agent = owned_agent(client, db, headers)
    version = create_random_agent_version(db, agent)
    url = "/api/v1/evals/runs"
    body = {"agent_version_id": str(version.id)}
    assert client.post(url, headers=headers, json=body).status_code == 400
    case = client.post(
        f"/api/v1/evals/cases?agent_id={agent.id}",
        headers=headers,
        json={"name": "one", "input": {"message": "hello"}, "tags": ["smoke"]},
    ).json()
    assert (
        client.post(
            url, headers=headers, json={**body, "case_ids": [str(uuid.uuid4())]}
        ).status_code
        == 400
    )
    pool = AsyncMock()
    monkeypatch.setattr(evals, "get_arq_pool", AsyncMock(return_value=pool))
    response = client.post(url, headers=headers, json={**body, "tags": ["smoke"]})
    assert response.status_code == 202
    batch = response.json()
    assert batch["case_count"] == 1
    assert (
        client.delete(f"/api/v1/evals/cases/{case['id']}", headers=headers).status_code
        == 409
    )
    assert client.get(url, headers=headers).json()["count"] >= 1
    assert (
        client.get(f"{url}/{batch['id']}/results", headers=headers).json()["count"] == 0
    )
    before_cancel = evals.evals_total.labels(status="cancelled")._value.get()
    assert (
        client.post(f"{url}/{batch['id']}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )
    assert (
        client.post(f"{url}/{batch['id']}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )
    assert (
        evals.evals_total.labels(status="cancelled")._value.get() == before_cancel + 1
    )
    assert (
        client.get(f"{url}/{batch['id']}", headers=headers).json()["status"]
        == "cancelled"
    )
    pool.enqueue_job.side_effect = ConnectionError("offline")
    before_failed = evals.evals_total.labels(status="failed")._value.get()
    assert client.post(url, headers=headers, json=body).status_code == 503
    assert evals.evals_total.labels(status="failed")._value.get() == before_failed + 1
    db.expire_all()
    assert any(
        b.status == "failed"
        for b in db.exec(
            select(EvalRun).where(EvalRun.agent_version_id == version.id)
        ).all()
    )


def test_comparison_and_permissions(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    left = create_eval_batch(db, 2)
    right = EvalRun(
        owner_id=left.owner_id,
        agent_version_id=left.agent_version_id,
        case_count=1,
        case_snapshots=left.case_snapshots[:1],
    )
    db.add(right)
    db.commit()
    db.refresh(right)
    db.add_all(
        [
            EvalResult(
                eval_run_id=left.id, eval_case_id=uuid.UUID(s["id"]), passed=True
            )
            for s in left.case_snapshots
        ]
    )
    db.add(
        EvalResult(
            eval_run_id=right.id,
            eval_case_id=uuid.UUID(left.case_snapshots[0]["id"]),
            passed=False,
        )
    )
    db.commit()
    url = f"/api/v1/evals/compare?left_id={left.id}&right_id={right.id}"
    data = client.get(url, headers=superuser_token_headers).json()
    assert data["regression_count"] == 1 and len(data["items"]) == 2
    assert any(
        item["right"] is None and item["verdict"] == "unchanged"
        for item in data["items"]
    )
    assert client.get(url, headers=normal_user_token_headers).status_code == 403
    assert (
        client.get(
            f"/api/v1/evals/runs/{left.id}", headers=normal_user_token_headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/evals/runs/{left.id}/results", headers=normal_user_token_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/evals/runs/{left.id}/cancel", headers=normal_user_token_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/evals/runs",
            headers=normal_user_token_headers,
            json={"agent_version_id": str(left.agent_version_id)},
        ).status_code
        == 403
    )
