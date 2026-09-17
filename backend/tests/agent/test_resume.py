import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlmodel import Session, select

from app import crud
from app.core.config import settings
from app.models import AgentUpdate, Run, RunEvent, User
from app.worker.tasks import execute_run_task
from tests.utils.agent import create_random_agent, create_random_agent_version
from tests.utils.tool import create_random_tool


@pytest.mark.parametrize("fresh", [False, True])
def test_resume_skips_completed_model_and_tool(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
    fresh: bool,
) -> None:
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    tool = create_random_tool(db, user.id)
    agent = create_random_agent(db, user.id)
    crud.update_agent(
        session=db, db_agent=agent, agent_in=AgentUpdate(tool_ids=[tool.id])
    )
    version = create_random_agent_version(db, agent)
    fake_chat_model.disable_streaming = True
    fake_chat_model.messages = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": tool.name, "args": {"expression": "2*21"}, "id": "calc"}
                ],
            ),
            AIMessage(content="42"),
        ]
    )
    original = GenericFakeChatModel.ainvoke
    calls = 0

    async def flaky(self: GenericFakeChatModel, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated crash after successful tool")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", flaky)
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **kw: self)
    response = client.post(
        "/api/v1/runs/async",
        headers=normal_user_token_headers,
        json={
            "agent_version_id": str(version.id),
            "input": {"message": "calculate"},
        },
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert client.portal is not None
    assert client.portal.call(execute_run_task, {}, run_id)["status"] == "failed"
    run = db.get(Run, uuid.UUID(run_id))
    assert run is not None and run.checkpoint_id is not None
    thread = run.thread_id
    if fresh:
        fake_chat_model.messages = iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": tool.name,
                            "args": {"expression": "2*21"},
                            "id": "fresh_calc",
                        }
                    ],
                ),
                AIMessage(content="42"),
            ]
        )
    retried = client.post(
        f"/api/v1/runs/{run_id}/retry",
        params={"fresh": fresh},
        headers=normal_user_token_headers,
    )
    assert retried.status_code == 202, retried.text
    assert client.portal.call(execute_run_task, {}, run_id)["status"] == "succeeded"
    db.refresh(run)
    assert (run.thread_id == thread) is (not fresh)
    assert run.output == {"content": "42"}
    # First attempt makes 2 calls (one fails); recovery makes only 1, not the
    # 2 calls required by a complete model -> tool -> model execution.
    assert calls == (4 if fresh else 3)
    events = db.exec(
        select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
    ).all()
    assert sum(e.event_type == "tool_called" for e in events) == (2 if fresh else 1)
    assert any(e.payload.get("resumed") is True for e in events) is (not fresh)
    assert [e.seq for e in events] == list(range(len(events)))
