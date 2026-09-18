import asyncio
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from app.agent.exceptions import AgentError, ModelCallError
from app.agent.guardrail import tools_requiring_approval
from app.agent.models import get_provider
from app.agent.state import AgentState


async def approval_gate(state: AgentState) -> dict[str, object]:
    """Pause before any tools run and pair rejected calls with tool results."""
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}
    required = set(state.get("approval_required_tools", []))
    pending = tools_requiring_approval(
        tool_calls=[dict(call) for call in last.tool_calls],
        approval_required_names=required,
    )
    if not pending:
        return {}
    decisions = interrupt({"type": "tool_approval", "tool_calls": pending})
    results: list[BaseMessage] = []
    for call in pending:
        decision = decisions.get(call["id"], {})
        if not decision.get("approved", False):
            reason = decision.get("reason") or "Rejected by user"
            results.append(
                ToolMessage(
                    content=f"Tool call was rejected by the user. Reason: {reason}. Do not retry this exact call. Consider an alternative approach or tell the user what you cannot do.",
                    tool_call_id=call["id"],
                    name=call["name"],
                )
            )
    return {"messages": results}


async def execute_tools(
    state: AgentState, *, tools: list[StructuredTool]
) -> dict[str, object]:
    """Execute unresolved calls concurrently, preserving artifacts and tool events."""
    messages = state["messages"]
    ai_index = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if isinstance(messages[i], AIMessage)
        ),
        None,
    )
    if ai_index is None:
        return {}
    last = messages[ai_index]
    assert isinstance(last, AIMessage)
    resolved = {
        m.tool_call_id for m in messages[ai_index + 1 :] if isinstance(m, ToolMessage)
    }
    tool_map = {tool.name: tool for tool in tools}

    async def invoke(call: dict[str, Any]) -> BaseMessage:
        tool = tool_map.get(call["name"])
        if tool is None:
            return ToolMessage(
                content=f"Unknown tool: {call['name']}",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
        try:
            result = await tool.ainvoke(
                {
                    "name": call["name"],
                    "args": call["args"],
                    "id": call["id"],
                    "type": "tool_call",
                }
            )
            if isinstance(result, ToolMessage):
                return result
            return ToolMessage(
                content=str(result), tool_call_id=call["id"], name=call["name"]
            )
        except Exception as exc:
            return ToolMessage(
                content=f"Tool execution failed: {str(exc)[:1000]}",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )

    results = await asyncio.gather(
        *(invoke(dict(call)) for call in last.tool_calls if call["id"] not in resolved)
    )
    return {"messages": list(results)}


async def call_model(
    state: AgentState, *, tools: list[StructuredTool] | None = None
) -> dict[str, object]:
    """Call the model without persisting the temporary system message."""
    messages: list[BaseMessage] = []
    if state["system_prompt"]:
        messages.append(SystemMessage(content=state["system_prompt"]))
    messages.extend(state["messages"])
    try:
        chat = get_provider().get_chat_model(
            model=state["llm_model"],
            temperature=state["llm_settings"].get("temperature"),
            max_tokens=state["llm_settings"].get("max_tokens"),
            streaming=True,
        )
        response = await (chat.bind_tools(tools) if tools else chat).ainvoke(messages)
        if not isinstance(response, AIMessage):
            raise ModelCallError("Model did not return an AI message")
    except AgentError:
        raise
    except Exception as exc:
        raise ModelCallError(str(exc)) from exc

    usage = response.usage_metadata
    return {
        "messages": [response],
        "iteration": state["iteration"] + 1,
        "prompt_tokens": state["prompt_tokens"]
        + (usage["input_tokens"] if usage else 0),
        "completion_tokens": state["completion_tokens"]
        + (usage["output_tokens"] if usage else 0),
    }
