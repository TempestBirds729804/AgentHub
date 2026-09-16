import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlmodel import Session

from app.agent.memory import (
    MAX_CONTEXT_MESSAGES,
    _as_text,
    build_context_messages,
    from_langchain_message,
    maybe_summarize,
    to_langchain_message,
)
from app.core.async_db import async_session_maker
from app.models import Conversation, ConvMessage, MessageRole
from tests.utils.conversation import create_random_conversation


@pytest.mark.parametrize(
    "message",
    [
        HumanMessage(content="hi"),
        AIMessage(
            content="call",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {"q": "x"},
                    "id": "call1",
                    "type": "tool_call",
                }
            ],
            usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        ),
        ToolMessage(content="result", tool_call_id="call1"),
        SystemMessage(content="instructions"),
    ],
)
def test_message_round_trip(message: Any) -> None:
    row = from_langchain_message(
        message, conversation_id=uuid.uuid4(), seq=1, run_id=None
    )
    restored = to_langchain_message(row)
    assert type(restored) is type(message)
    assert restored.content == message.content
    if isinstance(message, AIMessage):
        assert restored.tool_calls == message.tool_calls
        assert row.token_count == 2
    if isinstance(message, ToolMessage):
        assert restored.tool_call_id == "call1"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("abc", "abc"),
        (["a", {"type": "text", "text": "b"}], "ab"),
        ([{"type": "image_url", "image_url": "x"}], "[non-text content]"),
    ],
)
def test_text(content: Any, expected: str) -> None:
    assert _as_text(content) == expected


def test_context() -> None:
    history = [
        ConvMessage(
            conversation_id=uuid.uuid4(), seq=i, role=MessageRole.USER, content=str(i)
        )
        for i in range(40)
    ]
    messages = build_context_messages(
        history=history, summary="Earlier facts", system_prompt="Be concise"
    )
    assert len(messages) == MAX_CONTEXT_MESSAGES + 1
    assert isinstance(messages[0], SystemMessage)
    assert (
        messages[0].content
        == "Be concise\n\nSummary of earlier conversation:\nEarlier facts"
    )
    assert [msg.content for msg in messages[1:]] == [str(i) for i in range(20, 40)]
    assert build_context_messages(history=[], summary=None, system_prompt="") == []
    assert not isinstance(
        build_context_messages(history=history, summary=None, system_prompt="")[0],
        SystemMessage,
    )


@pytest.mark.parametrize(
    "scenario", ["below", "success", "failure", "already_summarized", "raced"]
)
@pytest.mark.usefixtures("fake_chat_model")
def test_summary(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    conv = create_random_conversation(db)
    conv.summary = "Original summary"
    conv.summarized_up_to_seq = 5 if scenario != "already_summarized" else 10
    db.add(conv)
    db.commit()
    conv_id = conv.id
    received: list[Any] = []

    async def capture(*args: Any, **_kwargs: Any) -> AIMessage:
        received.append(args)
        if scenario == "failure":
            raise RuntimeError("unavailable")
        if scenario == "raced":
            async with async_session_maker() as other_session:
                other = await other_session.get(Conversation, conv_id)
                assert other is not None
                other.sqlmodel_update(
                    {"summary": "Newer summary", "summarized_up_to_seq": 12}
                )
                other_session.add(other)
                await other_session.commit()
        return AIMessage(content="Updated summary")

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", capture)

    async def invoke() -> None:
        async with async_session_maker() as session:
            current = await session.get(Conversation, conv_id)
            assert current is not None
            history = [
                ConvMessage(
                    conversation_id=conv_id,
                    seq=i,
                    role=MessageRole.USER,
                    content=f"text-{i}",
                )
                for i in range(1, 30 if scenario == "below" else 31)
            ]
            await maybe_summarize(
                session=session, conversation=current, history=history
            )
            assert current.summary == (
                "Updated summary"
                if scenario == "success"
                else "Newer summary"
                if scenario == "raced"
                else "Original summary"
            )
            assert current.summarized_up_to_seq == (
                10
                if scenario in {"success", "already_summarized"}
                else 12
                if scenario == "raced"
                else 5
            )

    assert client.portal is not None
    client.portal.call(invoke)
    assert len(received) == (1 if scenario in {"success", "failure", "raced"} else 0)
    if scenario == "success":
        prompt = received[0][1][0].content
        assert (
            "Original summary" in prompt
            and "text-6" in prompt
            and "text-5\n" not in prompt
        )
