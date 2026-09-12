import uuid
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.graph import compile_agent_graph
from app.agent.state import AgentState


@pytest.mark.asyncio
async def test_graph_usage_and_system_prompt(
    fake_chat_model: GenericFakeChatModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = AIMessage(
        content="fake reply",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    )
    fake_chat_model.messages = iter([response])
    received: list[Any] = []
    original = GenericFakeChatModel.ainvoke

    async def capture(
        self: GenericFakeChatModel, input: Any, *args: Any, **kwargs: Any
    ) -> Any:
        received.extend(input)
        return await original(self, input, *args, **kwargs)

    monkeypatch.setattr(GenericFakeChatModel, "ainvoke", capture)
    state: AgentState = {
        "messages": [HumanMessage(content="Hello")],
        "run_id": uuid.uuid4(),
        "agent_version_id": uuid.uuid4(),
        "system_prompt": "Be concise.",
        "llm_model": "gpt-4o-mini",
        "llm_settings": {},
        "max_iterations": 10,
        "iteration": 0,
        "prompt_tokens": 20,
        "completion_tokens": 7,
    }
    result = await compile_agent_graph().ainvoke(state)
    assert result["iteration"] == 1
    assert result["prompt_tokens"] == 30
    assert result["completion_tokens"] == 12
    assert len(result["messages"]) == 2
    assert isinstance(result["messages"][-1], AIMessage)
    assert not any(isinstance(message, SystemMessage) for message in result["messages"])
    assert isinstance(received[0], SystemMessage)
    assert received[0].content == "Be concise."
