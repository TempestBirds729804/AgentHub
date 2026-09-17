from typing import Any, Literal

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from app.agent.nodes import call_model
from app.agent.state import AgentState


# ty cannot yet match TypedDict against LangGraph's StateLike protocol bound.
def build_agent_graph(
    tools: list[StructuredTool] | None = None,
) -> StateGraph[AgentState, None, AgentState, AgentState]:  # ty: ignore[invalid-type-arguments]
    """Build a bounded tool loop, or a linear graph when no tools are bound."""
    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)  # ty: ignore[invalid-type-arguments, invalid-argument-type, invalid-assignment]

    async def model_node(state: AgentState) -> dict[str, object]:
        return await call_model(state, tools=tools)

    graph.add_node("call_model", model_node)
    graph.add_edge(START, "call_model")
    if tools:
        graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
        graph.add_conditional_edges("call_model", should_continue)
        graph.add_edge("tools", "call_model")
    else:
        graph.add_edge("call_model", END)
    return graph


def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
    """Stop before executing further tools at the configured iteration limit."""
    if state["iteration"] >= state["max_iterations"]:
        return "__end__"
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "__end__"


def compile_agent_graph(
    tools: list[StructuredTool] | None = None,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[
    AgentState, None, AgentState, AgentState  # ty: ignore[invalid-type-arguments]
]:
    """Optionally persist graph state for worker recovery."""
    return build_agent_graph(tools).compile(checkpointer=checkpointer)
