from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.nodes import call_model
from app.agent.state import AgentState


# ty cannot yet match TypedDict against LangGraph's StateLike protocol bound.
def build_agent_graph() -> StateGraph[AgentState, None, AgentState, AgentState]:  # ty: ignore[invalid-type-arguments]
    """Build the phase 03 graph: START -> call_model -> END."""
    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)  # ty: ignore[invalid-type-arguments, invalid-argument-type, invalid-assignment]
    graph.add_node("call_model", call_model)
    graph.add_edge(START, "call_model")
    graph.add_edge("call_model", END)
    return graph


def compile_agent_graph() -> CompiledStateGraph[
    AgentState, None, AgentState, AgentState  # ty: ignore[invalid-type-arguments]
]:
    """Compile the graph; checkpointing is added in phase 06."""
    return build_agent_graph().compile()
