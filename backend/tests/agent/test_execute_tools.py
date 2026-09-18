import asyncio

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from app.agent.nodes import execute_tools
from tests.agent.test_graph_tools import initial_state


async def test_skips_resolved_and_reports_unknown() -> None:
    state = initial_state()
    state["messages"].extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "a", "name": "calc", "args": {}},
                    {"id": "b", "name": "unknown", "args": {}},
                ],
            ),
            ToolMessage(content="rejected", tool_call_id="a"),
        ]
    )

    async def forbidden() -> str:
        raise AssertionError("Rejected tool was executed")

    tool = StructuredTool.from_function(
        coroutine=forbidden, name="calc", description="test"
    )
    result = await execute_tools(state, tools=[tool])
    assert len(result["messages"]) == 1
    assert result["messages"][0].tool_call_id == "b"
    assert "Unknown tool" in result["messages"][0].content


async def test_tools_are_concurrent() -> None:
    started = 0
    both_started = asyncio.Event()

    async def work() -> str:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return "done"

    tool = StructuredTool.from_function(coroutine=work, name="work", description="test")
    state = initial_state()
    state["messages"].append(
        AIMessage(
            content="",
            tool_calls=[{"id": str(i), "name": "work", "args": {}} for i in range(2)],
        )
    )
    result = await execute_tools(state, tools=[tool])
    assert started == 2
    assert [m.content for m in result["messages"]] == ["done", "done"]
