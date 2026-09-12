import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlmodel import Session, select

from app.core.config import settings
from app.models import AgentVersion, Run, RunEvent, RunStatus, User
from tests.utils.agent import create_random_agent, create_random_agent_version
from tests.utils.run import create_random_run, create_random_run_event

URL = f"{settings.API_V1_STR}/runs/"


def version_for_user(db: Session) -> AgentVersion:
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    return create_random_agent_version(db, create_random_agent(db, owner_id=user.id))


def test_create_run(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
) -> None:
    fake_chat_model.messages = iter(
        [
            AIMessage(
                content="fake reply",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
            )
        ]
    )
    version = version_for_user(db)
    response = client.post(
        URL,
        headers=normal_user_token_headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "Hello"},
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "succeeded"
    assert run["output"]["content"] == "fake reply"
    assert run["prompt_tokens"] == 100 and run["completion_tokens"] == 50
    assert run["cost_usd"] == "0.000045"
    assert run["duration_ms"] > 0
    assert run["started_at"] and run["finished_at"]
    assert run["agent_name"] == version.snapshot["name"]
    assert run["agent_version_number"] == 1
    detail = client.get(URL + run["id"], headers=normal_user_token_headers)
    assert detail.json() == run
    events = client.get(
        URL + run["id"] + "/events", headers=normal_user_token_headers
    ).json()
    assert [e["event_type"] for e in events["data"]] == ["run_started", "run_finished"]
    assert [e["seq"] for e in events["data"]] == [0, 1]
    assert events["count"] == 2
    page = client.get(
        URL + run["id"] + "/events?skip=1&limit=1", headers=normal_user_token_headers
    ).json()
    assert page["count"] == 2 and page["data"][0]["seq"] == 1


@pytest.mark.parametrize("failure", ["model", "timeout", "persist"])
@pytest.mark.usefixtures("fake_chat_model")
def test_failed_run(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    version = version_for_user(db)
    version.snapshot = {**version.snapshot, "timeout_seconds": 1}
    db.add(version)
    db.commit()

    async def fail(*_args: Any, **_kwargs: Any) -> AIMessage:
        if failure == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("model unavailable")

    if failure != "persist":
        monkeypatch.setattr(GenericFakeChatModel, "ainvoke", fail)
    else:
        # A real constraint failure rolls back a flushed finish event; seq must
        # be resynced and expired Run attributes must be refreshed asynchronously.
        from app.services.run_service import RunEventRecorder

        original = RunEventRecorder.record

        async def invalid_event(
            self: RunEventRecorder, event_type: Any, **kwargs: Any
        ) -> None:
            await original(self, event_type, **kwargs)
            if event_type == "run_finished":
                await original(self, event_type, node_name="x" * 129)

        monkeypatch.setattr(RunEventRecorder, "record", invalid_event)
    response = client.post(
        URL,
        headers=normal_user_token_headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "Hello"},
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "failed" and run["error"]
    assert run["finished_at"] and run["duration_ms"] > 0
    if failure == "timeout":
        assert "timeout" in run["error"]
    events = client.get(
        URL + run["id"] + "/events", headers=normal_user_token_headers
    ).json()
    assert [e["event_type"] for e in events["data"]] == ["run_started", "run_failed"]
    assert [e["seq"] for e in events["data"]] == [0, 1]
    db.expire_all()
    assert db.get(Run, uuid.UUID(run["id"])).status == RunStatus.FAILED


def test_invalid_requests(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
) -> None:
    headers = normal_user_token_headers
    assert (
        client.post(
            URL,
            headers=headers,
            json={"agent_version_id": str(uuid.uuid4()), "input": {"message": "Hello"}},
        ).status_code
        == 404
    )
    foreign = create_random_agent_version(db, create_random_agent(db))
    assert (
        client.post(
            URL,
            headers=headers,
            json={"agent_version_id": str(foreign.id), "input": {"message": "Hello"}},
        ).status_code
        == 403
    )
    version = version_for_user(db)
    for value in ({}, {"message": None}, {"message": 123}, {"message": "  "}):
        assert (
            client.post(
                URL,
                headers=headers,
                json={"agent_version_id": str(version.id), "input": value},
            ).status_code
            == 422
        )
    version.snapshot = {"name": ["invalid"]}
    db.add(version)
    db.commit()
    assert (
        client.post(
            URL,
            headers=headers,
            json={"agent_version_id": str(version.id), "input": {"message": "Hello"}},
        ).status_code
        == 400
    )


def test_missing_key(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.models import get_provider

    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    get_provider.cache_clear()
    version = version_for_user(db)
    response = client.post(
        URL,
        headers=normal_user_token_headers,
        json={"agent_version_id": str(version.id), "input": {"message": "Hello"}},
    )
    assert response.status_code == 503
    assert "LLM_API_KEY" in response.json()["detail"]
    get_provider.cache_clear()


def test_listing_and_permissions(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    own_version = version_for_user(db)
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    own_runs = [
        Run(owner_id=user.id, agent_version_id=own_version.id, status=status)
        for status in (RunStatus.SUCCEEDED, RunStatus.FAILED)
    ]
    db.add_all(own_runs)
    db.commit()
    foreign_run = create_random_run(db)
    create_random_run_event(db, foreign_run)
    for suffix in ("", "/events"):
        assert (
            client.get(
                URL + str(foreign_run.id) + suffix, headers=normal_user_token_headers
            ).status_code
            == 403
        )
        assert (
            client.get(
                URL + str(foreign_run.id) + suffix, headers=superuser_token_headers
            ).status_code
            == 200
        )
        assert (
            client.get(
                URL + str(uuid.uuid4()) + suffix, headers=normal_user_token_headers
            ).status_code
            == 404
        )
    listing = client.get(URL, headers=normal_user_token_headers).json()
    assert all(run["owner_id"] == str(user.id) for run in listing["data"])
    filtered = client.get(
        URL,
        headers=normal_user_token_headers,
        params={"status": "failed", "agent_version_id": str(own_version.id)},
    ).json()
    assert filtered["count"] == 1 and filtered["data"][0]["status"] == "failed"
    paged = client.get(
        URL,
        headers=normal_user_token_headers,
        params={"agent_version_id": str(own_version.id), "skip": 1, "limit": 1},
    ).json()
    assert paged["count"] == 2 and len(paged["data"]) == 1
    admin = client.get(
        URL,
        headers=superuser_token_headers,
        params={"agent_version_id": str(foreign_run.agent_version_id)},
    ).json()
    assert admin["data"][0]["id"] == str(foreign_run.id)


def test_run_cascade(db: Session) -> None:
    run = create_random_run(db)
    event = create_random_run_event(db, run)
    run_id, event_id = run.id, event.id
    version = db.get(AgentVersion, run.agent_version_id)
    db.delete(version)
    db.commit()
    assert db.get(Run, run_id) is None
    assert db.get(RunEvent, event_id) is None
