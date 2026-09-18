import concurrent.futures

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import ApprovalRequest, Run
from tests.utils.approval import create_random_approval


def test_sse_cursor_skips_prior_pause(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    from app.core.redis import get_redis
    from app.models import RunEventType
    from app.services.event_bus import RedisEventPublisher

    request = create_random_approval(db)

    async def publish() -> str:
        publisher = RedisEventPublisher(redis=get_redis(), run_id=request.run_id)
        before: dict[str, object] = {"run_id": str(request.run_id)}
        await publisher.publish(RunEventType.APPROVAL_REQUESTED, before)
        await publisher.publish(RunEventType.APPROVAL_RESOLVED, {"approved": True})
        await publisher.publish(RunEventType.RUN_FINISHED, {"status": "succeeded"})
        return str(before["event_id"])

    assert client.portal is not None
    cursor = client.portal.call(publish)
    endpoint = f"/api/v1/runs/{request.run_id}/stream"
    response = client.get(
        endpoint, headers=superuser_token_headers, params={"last_event_id": cursor}
    )
    assert response.status_code == 200
    assert "event: approval_requested" not in response.text
    assert response.text.count("event: approval_resolved") == 1
    assert "id: " in response.text and "event: run_finished" in response.text
    replay = client.get(endpoint, headers=superuser_token_headers)
    assert (
        "event: approval_requested" in replay.text
        and "event: run_finished" in replay.text
    )
    assert (
        client.get(
            endpoint,
            headers=superuser_token_headers,
            params={"last_event_id": "invalid"},
        ).status_code
        == 422
    )


def test_list_permissions_reject_duplicate_cancel(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    first = create_random_approval(db)
    run = db.get(Run, first.run_id)
    second = create_random_approval(db, run)
    admin = superuser_token_headers
    url = f"/api/v1/approvals/{first.id}"
    assert client.get(url, headers=normal_user_token_headers).status_code == 403
    assert (
        client.post(
            url + "/decide", headers=normal_user_token_headers, json={"approved": True}
        ).status_code
        == 403
    )
    listed = client.get(
        "/api/v1/approvals/", headers=admin, params={"run_id": str(run.id)}
    ).json()
    assert listed["count"] == 2
    assert all(r["status"] == "pending" and r["agent_name"] for r in listed["data"])
    for reason in (None, "", "   "):
        assert (
            client.post(
                url + "/decide",
                headers=admin,
                json={"approved": False, "rejection_reason": reason},
            ).status_code
            == 422
        )
    response = client.post(
        url + "/decide",
        headers=admin,
        json={"approved": False, "rejection_reason": " too risky "},
    )
    assert (
        response.status_code == 200
        and response.json()["rejection_reason"] == "too risky"
    )
    assert (
        client.post(url + "/decide", headers=admin, json={"approved": True}).status_code
        == 409
    )
    assert (
        client.get(
            "/api/v1/approvals/",
            headers=admin,
            params={"run_id": str(run.id), "status": "rejected"},
        ).json()["count"]
        == 1
    )
    assert (
        client.get(
            "/api/v1/approvals/",
            headers=normal_user_token_headers,
            params={"run_id": str(run.id)},
        ).json()["count"]
        == 0
    )
    assert (
        client.post(f"/api/v1/runs/{run.id}/cancel", headers=admin).json()["status"]
        == "cancelled"
    )
    db.refresh(second)
    assert second.status == "expired"
    assert (
        client.post(
            f"/api/v1/approvals/{second.id}/decide",
            headers=admin,
            json={"approved": True},
        ).status_code
        == 409
    )


def test_parallel_decisions_enqueue_once(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import approvals

    first = create_random_approval(db)
    run = db.get(Run, first.run_id)
    second = create_random_approval(db, run)
    queued: list[str] = []

    class Pool:
        async def enqueue_job(
            self, _name: str, run_id: str, **_kwargs: object
        ) -> object:
            queued.append(run_id)
            return object()

    async def pool() -> Pool:
        return Pool()

    monkeypatch.setattr(approvals, "get_arq_pool", pool)

    def decide(id: object) -> int:
        return client.post(
            f"/api/v1/approvals/{id}/decide",
            headers=superuser_token_headers,
            json={"approved": True},
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        assert list(executor.map(decide, [first.id, second.id])) == [200, 200]
    assert queued == [str(run.id)]
    assert decide(first.id) == 409


def test_enqueue_failure_keeps_decision_and_fails_run(
    client: TestClient,
    db: Session,
    superuser_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import approvals

    request = create_random_approval(db)

    async def unavailable() -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(approvals, "get_arq_pool", unavailable)
    result = client.post(
        f"/api/v1/approvals/{request.id}/decide",
        headers=superuser_token_headers,
        json={"approved": True},
    )
    assert result.status_code == 503
    db.refresh(request)
    assert request.status == "approved"
    run = db.get(Run, request.run_id)
    assert run.status == "failed"
    assert (
        client.get(
            "/api/v1/approvals/",
            headers=superuser_token_headers,
            params={"run_id": str(run.id)},
        ).json()["count"]
        == 0
    )


def test_cancel_and_decide_race(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    request = create_random_approval(db)
    create_random_approval(db, db.get(Run, request.run_id))
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        decision = executor.submit(
            client.post,
            f"/api/v1/approvals/{request.id}/decide",
            headers=superuser_token_headers,
            json={"approved": True},
        )
        cancelled = executor.submit(
            client.post,
            f"/api/v1/runs/{request.run_id}/cancel",
            headers=superuser_token_headers,
        )
        assert cancelled.result().status_code == 200
        assert decision.result().status_code in (200, 409)
    db.expire_all()
    assert db.get(Run, request.run_id).status == "cancelled"
    assert db.get(ApprovalRequest, request.id).status in ("approved", "expired")
