import subprocess
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import text
from sqlmodel import Session

from app.agent.checkpoint import checkpointer_context, setup_checkpointer_once
from app.agent.graph import compile_agent_graph
from app.setup_checkpointer import main as setup_main
from tests.agent.test_graph_tools import initial_state


def test_checkpointer_entry_is_repeatable() -> None:
    setup_main()
    setup_main()


def test_checkpoint_setup_history_and_alembic(
    client: TestClient,
    db: Session,
    fake_chat_model: GenericFakeChatModel,
) -> None:
    fake_chat_model.messages = iter(
        [AIMessage(content="one"), AIMessage(content="two")]
    )
    thread = str(uuid.uuid4())

    async def invoke() -> None:
        await setup_checkpointer_once()
        await setup_checkpointer_once()
        async with checkpointer_context() as saver:
            graph = compile_agent_graph(checkpointer=saver)
            config = {"configurable": {"thread_id": thread}}
            await graph.ainvoke(initial_state(), config=config)
            second = initial_state()
            second["messages"] = [HumanMessage(content="second")]
            result = await graph.ainvoke(second, config=config)
            assert [m.content for m in result["messages"]] == [
                "calculate",
                "one",
                "second",
                "two",
            ]

    assert client.portal is not None
    client.portal.call(invoke)
    for name in (
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    ):
        assert db.execute(
            text("SELECT to_regclass(:name)"), {"name": name}
        ).scalar_one()
    assert (
        db.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id=:thread"),
            {"thread": thread},
        ).scalar_one()
        > 0
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "No new upgrade operations detected" in result.stdout
