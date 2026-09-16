import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlmodel import col, func, select

from app.agent.exceptions import ModelNotConfiguredError
from app.agent.memory import build_context_messages, maybe_summarize
from app.agent.models import get_provider
from app.agent.snapshot import AgentSnapshot
from app.api.deps import AsyncSessionDep, CurrentUser, SessionDep
from app.api.routes.agents import _get_owned_agent
from app.core.async_db import async_session_maker
from app.crud import conversation as crud
from app.models import (
    AgentVersion,
    Conversation,
    ConversationCreate,
    ConversationPublic,
    ConversationsPublic,
    ConversationUpdate,
    ConvMessage,
    ConvMessagePublic,
    ConvMessagesPublic,
    Message,
    MessageRole,
    Run,
    RunStatus,
    RunTrigger,
    StreamMessageRequest,
    get_datetime_utc,
)
from app.services.run_service import _cancel_run, stream_run

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _get_owned_conversation(
    *, session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Conversation:
    conversation = session.get(Conversation, id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not current_user.is_superuser and conversation.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return conversation


@router.get("/", response_model=ConversationsPublic)
def read_conversations(
    session: SessionDep,
    current_user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    agent_id: uuid.UUID | None = None,
) -> Any:
    """Retrieve conversations with message counts."""
    filters = []
    if not current_user.is_superuser:
        filters.append(Conversation.owner_id == current_user.id)
    if agent_id is not None:
        filters.append(Conversation.agent_id == agent_id)
    count = session.exec(
        select(func.count()).select_from(Conversation).where(*filters)
    ).one()
    conversations = session.exec(
        select(Conversation)
        .where(*filters)
        .order_by(col(Conversation.updated_at).desc(), col(Conversation.id))
        .offset(skip)
        .limit(limit)
    ).all()
    counts = dict(
        session.exec(
            select(ConvMessage.conversation_id, func.count())
            .where(
                col(ConvMessage.conversation_id).in_(
                    [conv.id for conv in conversations]
                )
            )
            .group_by(col(ConvMessage.conversation_id))
        ).all()
    )
    return ConversationsPublic(
        data=[
            ConversationPublic.model_validate(
                conv, update={"message_count": counts.get(conv.id, 0)}
            )
            for conv in conversations
        ],
        count=count,
    )


@router.post("/", response_model=ConversationPublic)
def create_conversation(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    conversation_in: ConversationCreate,
) -> Any:
    """Create an empty conversation for an accessible agent."""
    _get_owned_agent(
        session=session, current_user=current_user, agent_id=conversation_in.agent_id
    )
    return crud.create_conversation(
        session=session, conversation_in=conversation_in, owner_id=current_user.id
    )


@router.get("/{id}", response_model=ConversationPublic)
def read_conversation(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """Retrieve a conversation by ID."""
    return _get_owned_conversation(session=session, current_user=current_user, id=id)


@router.patch("/{id}", response_model=ConversationPublic)
def update_conversation(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    conversation_in: ConversationUpdate,
) -> Any:
    """Update a conversation title."""
    conversation = _get_owned_conversation(
        session=session, current_user=current_user, id=id
    )
    return crud.update_conversation(
        session=session, conversation=conversation, conversation_in=conversation_in
    )


@router.delete("/{id}", response_model=Message)
def delete_conversation(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """Delete a conversation and its messages while retaining runs."""
    conversation = _get_owned_conversation(
        session=session, current_user=current_user, id=id
    )
    session.refresh(conversation, with_for_update=True)
    active = session.exec(
        select(Run.id)
        .where(
            Run.conversation_id == id,
            col(Run.status).in_([RunStatus.QUEUED, RunStatus.RUNNING]),
        )
        .limit(1)
    ).first()
    if active is not None:
        raise HTTPException(status_code=409, detail="Conversation has an active run")
    session.delete(conversation)
    session.commit()
    return Message(message="Conversation deleted successfully")


@router.get("/{id}/messages", response_model=ConvMessagesPublic)
def read_messages(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Any:
    """Retrieve conversation messages in sequence order."""
    _get_owned_conversation(session=session, current_user=current_user, id=id)
    count = session.exec(
        select(func.count())
        .select_from(ConvMessage)
        .where(ConvMessage.conversation_id == id)
    ).one()
    messages = session.exec(
        select(ConvMessage)
        .where(ConvMessage.conversation_id == id)
        .order_by(col(ConvMessage.seq))
        .offset(skip)
        .limit(limit)
    ).all()
    return ConvMessagesPublic(
        data=[ConvMessagePublic.model_validate(msg) for msg in messages], count=count
    )


def _sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/{conversation_id}/stream", response_class=StreamingResponse)
async def stream_message(
    *,
    session: AsyncSessionDep,
    current_user: CurrentUser,
    conversation_id: uuid.UUID,
    body: StreamMessageRequest,
) -> Any:
    """Send a message and stream graph events using authenticated fetch SSE."""
    if not body.message.strip():
        raise HTTPException(
            status_code=422, detail="message must be a non-empty string"
        )
    conversation = await session.get(
        Conversation, conversation_id, with_for_update=True
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not current_user.is_superuser and conversation.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    active = (
        await session.execute(
            select(Run.id)
            .where(
                Run.conversation_id == conversation_id,
                col(Run.status).in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
            .limit(1)
        )
    ).first()
    if active is not None:
        raise HTTPException(status_code=409, detail="Conversation has an active run")
    version = (
        await session.execute(
            select(AgentVersion)
            .where(
                AgentVersion.agent_id == conversation.agent_id,
                col(AgentVersion.is_draft).is_(False),
            )
            .order_by(col(AgentVersion.version_number).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=400,
            detail="Publish an agent version before starting a conversation",
        )
    try:
        snapshot = AgentSnapshot.model_validate(version.snapshot)
        get_provider()
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid agent version snapshot")
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    history = list(
        (
            await session.execute(
                select(ConvMessage)
                .where(ConvMessage.conversation_id == conversation_id)
                .order_by(col(ConvMessage.seq))
            )
        )
        .scalars()
        .all()
    )
    run = Run(
        owner_id=conversation.owner_id,
        agent_version_id=version.id,
        conversation_id=conversation_id,
        thread_id=conversation.thread_id,
        trigger=RunTrigger.PLAYGROUND,
        input={"message": body.message},
    )
    user_message = ConvMessage(
        conversation_id=conversation_id,
        seq=history[-1].seq + 1 if history else 1,
        role=MessageRole.USER,
        content=body.message,
        run_id=run.id,
    )
    history.append(user_message)
    context = build_context_messages(
        history=history,
        summary=conversation.summary,
        system_prompt=snapshot.system_prompt,
    )
    conversation.sqlmodel_update(
        {
            "updated_at": get_datetime_utc(),
            "title": conversation.title or body.message[:50],
        }
    )
    session.add_all([run, user_message, conversation])
    await session.commit()
    await session.refresh(run)
    run_id = run.id

    async def events() -> AsyncGenerator[str]:
        # The response owns its session independently of dependency teardown.
        stream_session = async_session_maker()
        stream_record: Run | None = None
        try:
            stream_record = await stream_session.get(Run, run_id)
            if stream_record is None:
                return
            async with aclosing(
                stream_run(
                    session=stream_session,
                    run=stream_record,
                    snapshot=snapshot,
                    context_messages=context,
                )
            ) as stream:
                async for event_type, payload in stream:
                    yield _sse(event_type.value, payload)
            if stream_record.status == RunStatus.SUCCEEDED:
                current = await stream_session.get(Conversation, conversation_id)
                if current is not None:
                    messages = list(
                        (
                            await stream_session.execute(
                                select(ConvMessage)
                                .where(ConvMessage.conversation_id == conversation_id)
                                .order_by(col(ConvMessage.seq))
                            )
                        )
                        .scalars()
                        .all()
                    )
                    await maybe_summarize(
                        session=stream_session, conversation=current, history=messages
                    )
        except asyncio.CancelledError:
            if stream_record is None:
                with anyio.CancelScope(shield=True):
                    await stream_session.rollback()
                    stream_record = await stream_session.get(Run, run_id)
            if stream_record is not None:
                await _cancel_run(session=stream_session, run=stream_record)
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await stream_session.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
