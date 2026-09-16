from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from app.agent.exceptions import AgentError, ModelCallError
from app.agent.models import get_provider
from app.agent.state import AgentState


async def call_model(state: AgentState) -> dict[str, object]:
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
        response = await chat.ainvoke(messages)
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
