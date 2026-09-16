import asyncio
from contextlib import aclosing
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage
from sqlmodel import Session, select

from app.agent.snapshot import AgentSnapshot
from app.core.async_db import async_session_maker
from app.models import AgentVersion, Run, RunEvent, RunStatus
from app.services.run_service import stream_run
from tests.utils.conversation import create_random_conversation


@pytest.mark.parametrize("mode", ["cancel", "close"])
@pytest.mark.usefixtures("fake_chat_model")
def test_stream_cancellation(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    conv = create_random_conversation(db)
    version = db.exec(
        select(AgentVersion).where(AgentVersion.agent_id == conv.agent_id)
    ).one()
    run = Run(
        owner_id=conv.owner_id, agent_version_id=version.id, conversation_id=conv.id
    )
    db.add(run)
    db.commit()
    run_id = run.id
    snapshot = AgentSnapshot.model_validate(version.snapshot)

    async def slow(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(20)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", slow)

    async def invoke() -> None:
        async with async_session_maker() as session:
            current = await session.get(Run, run_id)
            assert current is not None
            async with aclosing(
                stream_run(
                    session=session,
                    run=current,
                    snapshot=snapshot,
                    context_messages=[HumanMessage(content="hi")],
                )
            ) as stream:
                if mode == "close":
                    await anext(stream)
                else:
                    with anyio.move_on_after(0.2) as cancel_scope:
                        async for _ in stream:
                            pass
                    assert cancel_scope.cancel_called

    assert client.portal is not None
    client.portal.call(invoke)
    db.refresh(run)
    assert run.status == RunStatus.CANCELLED
    events = db.exec(
        select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
    ).all()
    assert (
        events[-1].event_type == "run_finished"
        and events[-1].payload["status"] == "cancelled"
    )
