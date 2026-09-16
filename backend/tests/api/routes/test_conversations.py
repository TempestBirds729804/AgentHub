import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    AgentVersion,
    Conversation,
    ConvMessage,
    Run,
    RunEvent,
    RunStatus,
    User,
)
from tests.utils.conversation import create_random_conversation
from tests.utils.sse import _parse_sse

URL = f"{settings.API_V1_STR}/conversations/"


def own_conversation(db: Session, *, published: bool = True) -> Conversation:
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    return create_random_conversation(db, owner_id=user.id, published=published)


def test_stream_and_memory(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_chat_model.messages = iter(
        [AIMessage(content="first reply"), AIMessage(content="second reply")]
    )
    seen: list[Any] = []
    original = GenericFakeChatModel.ainvoke

    async def capture(
        self: GenericFakeChatModel, input: Any, *args: Any, **kwargs: Any
    ) -> Any:
        seen.append(input)
        return await original(self, input, *args, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", capture)
    conv = own_conversation(db)
    for index, text in enumerate(["My name is Zhang San", "What is my name?"]):
        with client.stream(
            "POST",
            URL + str(conv.id) + "/stream",
            headers=normal_user_token_headers,
            json={"message": text},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            events = _parse_sse("".join(response.iter_text()))
        assert events[0]["event"] == "run_started"
        assert events[-1]["event"] == "run_finished", events
        assert sum(event["event"] == "model_chunk" for event in events) > 1
        assert [
            event["event"] for event in events if event["event"] != "model_chunk"
        ] == ["run_started", "node_started", "node_finished", "run_finished"]
        run = db.get(Run, uuid.UUID(events[0]["data"]["run_id"]))
        assert (
            run is not None
            and run.status == RunStatus.SUCCEEDED
            and run.conversation_id == conv.id
        )
        assert not db.exec(
            select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.event_type == "model_chunk"
            )
        ).all()
        listing = client.get(
            URL + str(conv.id) + "/messages", headers=normal_user_token_headers
        ).json()
        assert listing["count"] == (index + 1) * 2
        assert [message["seq"] for message in listing["data"]] == list(
            range(1, (index + 1) * 2 + 1)
        )
        assert listing["data"][-1]["id"] == events[-1]["data"]["message_id"]
    assert [message.content for message in seen[1]][1:] == [
        "My name is Zhang San",
        "first reply",
        "What is my name?",
    ]
    assert (
        client.get(URL + str(conv.id), headers=normal_user_token_headers).json()[
            "title"
        ]
        == "My name is Zhang San"
    )


@pytest.mark.parametrize("failure", ["model", "timeout", "persist"])
@pytest.mark.usefixtures("fake_chat_model")
def test_stream_failure(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    conv = own_conversation(db)
    version = db.exec(
        select(AgentVersion).where(AgentVersion.agent_id == conv.agent_id)
    ).one()
    version.snapshot = {**version.snapshot, "timeout_seconds": 1}
    db.add(version)
    db.commit()

    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        if failure == "timeout":
            await asyncio.sleep(5)
        raise RuntimeError("model unavailable")

    if failure != "persist":
        monkeypatch.setattr(GenericFakeChatModel, "ainvoke", fail)
    else:
        from app.services.run_service import RunEventRecorder

        original = RunEventRecorder.record

        async def invalid(
            self: RunEventRecorder, event_type: Any, **kwargs: Any
        ) -> None:
            await original(self, event_type, **kwargs)
            if event_type == "run_finished":
                await original(self, event_type, node_name="x" * 129)

        monkeypatch.setattr(RunEventRecorder, "record", invalid)
    response = client.post(
        URL + str(conv.id) + "/stream",
        headers=normal_user_token_headers,
        json={"message": "hello"},
    )
    events = _parse_sse(response.text)
    assert events[-1]["event"] == "run_failed", events
    run = db.get(Run, uuid.UUID(events[-1]["data"]["run_id"]))
    assert run is not None and run.status == RunStatus.FAILED
    assert (
        len(
            db.exec(
                select(ConvMessage).where(ConvMessage.conversation_id == conv.id)
            ).all()
        )
        == 1
    )


@pytest.mark.usefixtures("fake_chat_model")
def test_crud_permissions_and_cascade(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    superuser_token_headers: dict[str, str],
) -> None:
    headers = normal_user_token_headers
    conv = own_conversation(db)
    created = client.post(
        URL,
        headers=headers,
        json={"agent_id": str(conv.agent_id), "title": "temporary"},
    )
    assert created.status_code == 200
    target = URL + created.json()["id"]
    assert (
        client.patch(target, headers=headers, json={"title": "renamed"}).json()["title"]
        == "renamed"
    )
    response = client.post(
        target + "/stream", headers=headers, json={"message": "hello"}
    )
    run_id = uuid.UUID(_parse_sse(response.text)[0]["data"]["run_id"])
    listing = client.get(
        URL, headers=headers, params={"agent_id": str(conv.agent_id), "limit": 1}
    ).json()
    assert listing["count"] == 2 and listing["data"][0]["message_count"] == 2
    page = client.get(target + "/messages?skip=1&limit=1", headers=headers).json()
    assert page["count"] == 2 and page["data"][0]["role"] == "assistant"
    assert client.delete(target, headers=headers).status_code == 200
    db.expire_all()
    assert db.get(Run, run_id).conversation_id is None
    assert not db.exec(
        select(ConvMessage).where(
            ConvMessage.conversation_id == uuid.UUID(created.json()["id"])
        )
    ).all()
    foreign = create_random_conversation(db)
    foreign_url = URL + str(foreign.id)
    for method, suffix, body in [
        ("GET", "", None),
        ("GET", "/messages", None),
        ("PATCH", "", {"title": "x"}),
        ("DELETE", "", None),
        ("POST", "/stream", {"message": "x"}),
    ]:
        assert (
            client.request(
                method, foreign_url + suffix, headers=headers, json=body
            ).status_code
            == 403
        )
    assert client.get(foreign_url, headers=superuser_token_headers).status_code == 200
    assert (
        client.post(
            URL, headers=headers, json={"agent_id": str(foreign.agent_id)}
        ).status_code
        == 403
    )
    assert all(
        item["owner_id"] == str(conv.owner_id)
        for item in client.get(URL, headers=headers).json()["data"]
    )


@pytest.mark.usefixtures("fake_chat_model")
def test_invalid_stream_requests(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
) -> None:
    conv = own_conversation(db, published=False)
    target = URL + str(conv.id) + "/stream"
    assert (
        client.post(
            target, headers=normal_user_token_headers, json={"message": "hi"}
        ).status_code
        == 400
    )
    for message in ["", " ", "x" * 20001]:
        assert (
            client.post(
                target, headers=normal_user_token_headers, json={"message": message}
            ).status_code
            == 422
        )
    assert (
        client.post(
            URL + str(uuid.uuid4()) + "/stream",
            headers=normal_user_token_headers,
            json={"message": "hi"},
        ).status_code
        == 404
    )
    assert not db.exec(
        select(ConvMessage).where(ConvMessage.conversation_id == conv.id)
    ).all()


def test_published_version_long_reply_and_active_run(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
) -> None:
    conv = own_conversation(db)
    published = db.exec(
        select(AgentVersion).where(AgentVersion.agent_id == conv.agent_id)
    ).one()
    draft = AgentVersion(
        agent_id=conv.agent_id,
        version_number=2,
        is_draft=True,
        snapshot={"invalid": True},
    )
    db.add(draft)
    active = Run(
        owner_id=conv.owner_id, agent_version_id=published.id, conversation_id=conv.id
    )
    db.add(active)
    db.commit()
    target = URL + str(conv.id)
    assert (
        client.post(
            target + "/stream",
            headers=normal_user_token_headers,
            json={"message": "hi"},
        ).status_code
        == 409
    )
    assert client.delete(target, headers=normal_user_token_headers).status_code == 409
    active.status = RunStatus.CANCELLED
    db.add(active)
    db.commit()
    content = "长回复" * 10000
    fake_chat_model.messages = iter([AIMessage(content=content)])
    response = client.post(
        target + "/stream", headers=normal_user_token_headers, json={"message": "hi"}
    )
    events = _parse_sse(response.text)
    assert events[-1]["event"] == "run_finished"
    run = db.get(Run, uuid.UUID(events[0]["data"]["run_id"]))
    assert run.agent_version_id == published.id
    assert (
        client.get(target + "/messages", headers=normal_user_token_headers).json()[
            "data"
        ][-1]["content"]
        == content
    )


@pytest.mark.usefixtures("fake_chat_model")
def test_run_conversation_validation(
    client: TestClient, db: Session, normal_user_token_headers: dict[str, str]
) -> None:
    conv = own_conversation(db)
    other = own_conversation(db)
    foreign = create_random_conversation(db)
    version = db.exec(
        select(AgentVersion).where(AgentVersion.agent_id == conv.agent_id)
    ).one()
    for conversation_id, expected in [
        (uuid.uuid4(), 404),
        (other.id, 400),
        (foreign.id, 403),
    ]:
        response = client.post(
            f"{settings.API_V1_STR}/runs/",
            headers=normal_user_token_headers,
            json={
                "agent_version_id": str(version.id),
                "conversation_id": str(conversation_id),
                "input": {"message": "hi"},
            },
        )
        assert response.status_code == expected
