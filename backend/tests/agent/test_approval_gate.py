import uuid

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.nodes import approval_gate
from app.agent.state import AgentState
from tests.agent.test_graph_tools import initial_state


async def test_gate_without_calls_or_required_tools() -> None:
    state = initial_state()
    assert await approval_gate(state) == {}
    state["messages"].append(
        AIMessage(content="", tool_calls=[{"id": "a", "name": "calc", "args": {}}])
    )
    assert await approval_gate(state) == {}


async def test_gate_checkpoint_and_rejection() -> None:
    graph = StateGraph(AgentState)
    graph.add_node("gate", approval_gate)
    graph.add_edge(START, "gate")
    graph.add_edge("gate", END)
    app = graph.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    state = initial_state()
    state["approval_required_tools"] = ["calc"]
    state["messages"].append(
        AIMessage(content="", tool_calls=[{"id": "a", "name": "calc", "args": {}}])
    )
    await app.ainvoke(state, config)
    saved = await app.aget_state(config)
    assert saved.next == ("gate",)
    assert saved.tasks[0].interrupts[0].value["tool_calls"][0]["id"] == "a"
    assert saved.values["approval_required_tools"] == ["calc"]
    result = await app.ainvoke(
        Command(resume={"a": {"approved": False, "reason": "too risky"}}), config
    )
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "a"
    assert "too risky" in message.content and "Do not retry" in message.content
