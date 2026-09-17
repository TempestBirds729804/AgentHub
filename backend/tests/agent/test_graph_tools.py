import asyncio
import itertools
import uuid
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

from app.agent.graph import compile_agent_graph, should_continue
from app.agent.state import AgentState
from app.agent.tools.adapter import to_langchain_tool
from app.agent.tools.function import get_function
from app.models import Tool, ToolType


def initial_state(limit: int = 3) -> AgentState:
    return {
        "messages": [HumanMessage(content="calculate")],
        "run_id": uuid.uuid4(),
        "agent_version_id": uuid.uuid4(),
        "system_prompt": "",
        "llm_model": "fake",
        "llm_settings": {},
        "max_iterations": limit,
        "iteration": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def tool_reply() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "calculator",
                "args": {"expression": "1+1"},
                "id": str(uuid.uuid4()),
            }
        ],
    )


def test_conditional_edges() -> None:
    state = initial_state()
    assert should_continue(state) == END
    state["messages"].append(tool_reply())
    assert should_continue(state) == "tools"
    state["iteration"] = 3
    assert should_continue(state) == END
    graph = compile_agent_graph().get_graph()
    assert any(
        edge.source == "call_model" and edge.target == END for edge in graph.edges
    )


@pytest.mark.parametrize("loop", [False, True])
async def test_tool_loop(
    loop: bool, fake_chat_model: GenericFakeChatModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    fn = get_function("calculator")
    assert fn
    tool = Tool(
        name="calculator",
        description=fn.description,
        owner_id=uuid.uuid4(),
        tool_type=ToolType.FUNCTION,
        config={"function_name": "calculator"},
        parameters_schema=fn.args_model.model_json_schema(),
    )

    def bind(
        self: GenericFakeChatModel, *_args: Any, **_kwargs: Any
    ) -> GenericFakeChatModel:
        return self

    monkeypatch.setattr(GenericFakeChatModel, "bind_tools", bind)
    fake_chat_model.messages = (
        (tool_reply() for _ in itertools.count())
        if loop
        else iter([tool_reply(), AIMessage(content="2")])
    )
    result = await asyncio.wait_for(
        compile_agent_graph([to_langchain_tool(tool)]).ainvoke(initial_state()), 3
    )
    assert result["iteration"] == (3 if loop else 2)
    assert isinstance(result["messages"][2], ToolMessage)
    assert result["messages"][2].content == "2.0"
    if not loop:
        assert [type(m) for m in result["messages"]][1:] == [
            AIMessage,
            ToolMessage,
            AIMessage,
        ]
