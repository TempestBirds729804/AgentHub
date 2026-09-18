import uuid
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Graph state with message append/deduplication and accumulated usage."""

    messages: Annotated[list[BaseMessage], add_messages]
    run_id: uuid.UUID
    agent_version_id: uuid.UUID
    system_prompt: str
    llm_model: str
    llm_settings: dict[str, Any]
    max_iterations: int
    iteration: int
    prompt_tokens: int
    completion_tokens: int
    approval_required_tools: NotRequired[list[str]]
    knowledge_base_ids: NotRequired[list[str]]
    retrieved_chunks: NotRequired[list[dict[str, Any]]]
