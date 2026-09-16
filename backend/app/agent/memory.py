import logging
import uuid
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import get_provider
from app.core.config import settings
from app.models import Conversation, ConvMessage, MessageRole

logger = logging.getLogger(__name__)
MAX_CONTEXT_MESSAGES = 20
SUMMARIZE_THRESHOLD = 30


def _as_text(content: str | list[str | dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block
        if isinstance(block, str)
        else str(block.get("text", ""))
        if block.get("type") == "text"
        else "[non-text content]"
        for block in content
    )


def to_langchain_message(msg: ConvMessage) -> BaseMessage:
    if msg.role == MessageRole.USER:
        return HumanMessage(content=msg.content)
    if msg.role == MessageRole.ASSISTANT:
        return AIMessage(content=msg.content, tool_calls=msg.tool_calls or [])
    if msg.role == MessageRole.TOOL:
        return ToolMessage(content=msg.content, tool_call_id=msg.tool_call_id or "")
    return SystemMessage(content=msg.content)


def from_langchain_message(
    msg: BaseMessage, *, conversation_id: uuid.UUID, seq: int, run_id: uuid.UUID | None
) -> ConvMessage:
    role = MessageRole.SYSTEM
    if isinstance(msg, HumanMessage):
        role = MessageRole.USER
    elif isinstance(msg, AIMessage):
        role = MessageRole.ASSISTANT
    elif isinstance(msg, ToolMessage):
        role = MessageRole.TOOL
    usage = getattr(msg, "usage_metadata", None) or {}
    return ConvMessage(
        conversation_id=conversation_id,
        seq=seq,
        run_id=run_id,
        role=role,
        content=_as_text(msg.content),
        tool_calls=getattr(msg, "tool_calls", []) or [],
        tool_call_id=getattr(msg, "tool_call_id", None),
        token_count=usage.get("output_tokens"),
    )


def build_context_messages(
    *, history: list[ConvMessage], summary: str | None, system_prompt: str
) -> list[BaseMessage]:
    parts = [system_prompt] if system_prompt else []
    if summary:
        parts.append(f"Summary of earlier conversation:\n{summary}")
    messages: list[BaseMessage] = (
        [SystemMessage(content="\n\n".join(parts))] if parts else []
    )
    messages.extend(
        to_langchain_message(msg) for msg in history[-MAX_CONTEXT_MESSAGES:]
    )
    return messages


async def maybe_summarize(
    *, session: AsyncSession, conversation: Conversation, history: list[ConvMessage]
) -> None:
    if len(history) < SUMMARIZE_THRESHOLD:
        return
    earlier = [
        msg
        for msg in history[:-MAX_CONTEXT_MESSAGES]
        if msg.seq
        > (
            conversation.summarized_up_to_seq
            if conversation.summarized_up_to_seq is not None
            else -1
        )
    ]
    if not earlier:
        return
    conversation_id = conversation.id
    previous_cutoff = conversation.summarized_up_to_seq
    transcript = "\n".join(
        f"{MessageRole(msg.role).value}: {msg.content}" for msg in earlier
    )
    prompt = (
        "Summarize in at most 200 words. Keep facts, decisions and user preferences. "
        "Extend the existing summary with the new excerpt.\n\n"
        f"Existing summary:\n{conversation.summary or ''}\n\nNew excerpt:\n{transcript}"
    )
    try:
        chat = get_provider().get_chat_model(model=settings.LLM_MODEL, temperature=0)
        response = await chat.ainvoke([HumanMessage(content=prompt)])
        # A later turn may have summarized while this model call was in flight.
        await session.refresh(conversation, with_for_update=True)
        if conversation.summarized_up_to_seq != previous_cutoff:
            await session.commit()
            return
        conversation.sqlmodel_update(
            {
                "summary": _as_text(response.content)[:8000],
                "summarized_up_to_seq": earlier[-1].seq,
            }
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
    except Exception:
        await session.rollback()
        await session.refresh(conversation)
        logger.warning(
            "Failed to summarize conversation %s", conversation_id, exc_info=True
        )
