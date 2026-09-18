import asyncio
import uuid
from typing import Any

import pytest
from arq.jobs import Job
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from sqlmodel import Session, select

from app import crud
from app.agent.tools.function import FunctionToolExecutor
from app.core.config import settings
from app.models import AgentUpdate, ApprovalRequest, Run, RunEvent, User
from app.worker.settings import get_arq_pool
from app.worker.tasks import execute_run_task, resume_job_id, resume_run_task
from tests.utils.agent import create_random_agent, create_random_agent_version
from tests.utils.tool import create_random_tool


@pytest.mark.parametrize(
    "approved,parallel,second_round,entry",
    [
        (True, False, False, "async"),
        (False, False, False, "async"),
        (True, True, False, "async"),
        (False, True, False, "async"),
        (True, False, True, "async"),
        (True, False, False, "stream"),
        (False, False, False, "stream"),
        (True, False, False, "sync"),
        (True, False, False, "redelivery"),
    ],
)
def test_approval_flow(
    client: TestClient,
    db: Session,
    normal_user_token_headers: dict[str, str],
    fake_chat_model: GenericFakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
    parallel: bool,
    second_round: bool,
    entry: str,
) -> None:
    headers = normal_user_token_headers
    user = db.exec(select(User).where(User.email == settings.EMAIL_TEST_USER)).one()
    tool = create_random_tool(db, user.id)
    tool.sqlmodel_update({"requires_approval": True})
    db.add(tool)
    db.commit()
    agent = create_random_agent(db, user.id)
    crud.update_agent(
        session=db, db_agent=agent, agent_in=AgentUpdate(tool_ids=[tool.id])
    )
    version = create_random_agent_version(db, agent)
    count = 2 if parallel else 1
    calls = [
        {"name": tool.name, "id": f"call-{i}", "args": {"expression": "2+2"}}
        for i in range(count)
    ]
    replies = [AIMessage(content="", tool_calls=calls)]
    if second_round:
        replies.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool.name,
                        "id": "round-two",
                        "args": {"expression": "3+3"},
                    }
                ],
            )
        )
    replies.append(AIMessage(content="done"))
    fake_chat_model.messages = iter(replies)
    fake_chat_model.disable_streaming = True
    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", lambda self, *a, **kw: self)
    model_inputs: list[Any] = []
    original_invoke = GenericFakeChatModel.ainvoke
    model_blocked = asyncio.Event()

    async def capture(
        self: GenericFakeChatModel, value: Any, *args: Any, **kwargs: Any
    ) -> Any:
        model_inputs.append(value)
        if entry == "redelivery" and len(model_inputs) == 2:
            model_blocked.set()
            await asyncio.Event().wait()
        return await original_invoke(self, value, *args, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", capture)
    executions = 0
    original_execute = FunctionToolExecutor.execute

    async def execute(self: FunctionToolExecutor, **kwargs: Any) -> Any:
        nonlocal executions
        executions += 1
        return await original_execute(self, **kwargs)

    monkeypatch.setattr(FunctionToolExecutor, "execute", execute)
    assert client.portal is not None
    conversation_id = None
    if entry == "stream":
        conversation_id = client.post(
            "/api/v1/conversations/", headers=headers, json={"agent_id": str(agent.id)}
        ).json()["id"]
        response = client.post(
            f"/api/v1/conversations/{conversation_id}/stream",
            headers=headers,
            json={"message": "calculate"},
        )
        assert (
            response.status_code == 200 and "event: approval_requested" in response.text
        ), response.text
        run = db.exec(
            select(Run).where(Run.conversation_id == uuid.UUID(conversation_id))
        ).one()
        assert (
            client.post(
                f"/api/v1/conversations/{conversation_id}/stream",
                headers=headers,
                json={"message": "again"},
            ).status_code
            == 409
        )
    else:
        response = client.post(
            "/api/v1/runs/async"
            if entry in ("async", "redelivery")
            else "/api/v1/runs/",
            headers=headers,
            json={
                "agent_version_id": str(version.id),
                "input": {"message": "calculate"},
            },
        )
        assert response.status_code == (
            202 if entry in ("async", "redelivery") else 200
        ), response.text
        run = db.get(Run, uuid.UUID(response.json()["id"]))
        if entry in ("async", "redelivery"):
            assert (
                client.portal.call(execute_run_task, {}, str(run.id))["status"]
                == "waiting_approval"
            )
    db.refresh(run)
    assert run.status == "waiting_approval" and executions == 0
    requests = db.exec(
        select(ApprovalRequest).where(ApprovalRequest.run_id == run.id)
    ).all()
    assert len(requests) == count
    for i, request in enumerate(requests):
        result = client.post(
            f"/api/v1/approvals/{request.id}/decide",
            headers=headers,
            json={"approved": approved, "rejection_reason": "too risky"},
        )
        assert result.status_code == 200, result.text
        db.refresh(run)

        async def queued() -> bool:
            return (
                await Job(resume_job_id(run), await get_arq_pool()).info() is not None
            )

        assert client.portal.call(queued) is (i == count - 1)
        assert run.status == ("running" if i == count - 1 else "waiting_approval")
    assert executions == 0
    resume_context = {"job_id": resume_job_id(run)}
    if entry == "redelivery":
        paused_checkpoint = run.checkpoint_id

        async def interrupt_resume() -> None:
            from app.core.async_db import async_session_maker

            task = asyncio.create_task(resume_run_task(resume_context, str(run.id)))
            await asyncio.wait_for(model_blocked.wait(), 5)
            async with asyncio.timeout(5):
                while True:
                    async with async_session_maker() as observer:
                        recorded = (
                            await observer.execute(
                                select(RunEvent.id).where(
                                    RunEvent.run_id == run.id,
                                    RunEvent.event_type == "tool_result",
                                )
                            )
                        ).first()
                    if recorded is not None:
                        break
                    await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        client.portal.call(interrupt_resume)
        db.refresh(run)
        assert run.status == "running"
        assert run.checkpoint_id != paused_checkpoint
        assert executions == 1
    assert client.portal.call(resume_run_task, resume_context, str(run.id))[
        "status"
    ] == ("waiting_approval" if second_round else "succeeded")
    if second_round:
        request = db.exec(
            select(ApprovalRequest).where(
                ApprovalRequest.run_id == run.id, ApprovalRequest.status == "pending"
            )
        ).one()
        assert request.tool_call_id == "round-two"
        assert (
            client.post(
                f"/api/v1/approvals/{request.id}/decide",
                headers=headers,
                json={"approved": True},
            ).status_code
            == 200
        )
        assert (
            client.portal.call(resume_run_task, {}, str(run.id))["status"]
            == "succeeded"
        )
    db.refresh(run)
    assert run.status == "succeeded"
    assert executions == (count + int(second_round) if approved else 0)
    if not approved:
        rejected = [m for m in model_inputs[-1] if isinstance(m, ToolMessage)]
        assert len(rejected) == count
        assert all(
            "rejected" in m.content and "too risky" in m.content for m in rejected
        )
    events = db.exec(
        select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
    ).all()
    assert [e.seq for e in events] == list(range(len(events)))
    assert any(e.event_type == "approval_requested" for e in events)
    assert any(e.event_type == "approval_resolved" for e in events)
    assert sum(e.event_type == "tool_called" for e in events) == executions
    terminal = client.get(f"/api/v1/runs/{run.id}/stream", headers=headers)
    assert '"content": "done"' in terminal.text
