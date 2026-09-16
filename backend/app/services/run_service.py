import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import anyio
from langchain_core.messages import BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.agent.exceptions import AgentError, AgentTimeoutError
from app.agent.graph import compile_agent_graph
from app.agent.memory import _as_text, from_langchain_message
from app.agent.pricing import estimate_cost_usd
from app.agent.snapshot import AgentSnapshot
from app.agent.state import AgentState
from app.models import (
    Conversation,
    ConvMessage,
    Run,
    RunEvent,
    RunEventType,
    RunStatus,
    get_datetime_utc,
)

KNOWN_NODE_NAMES = {"call_model"}


class RunEventRecorder:
    """Record strictly increasing sequence numbers within one run."""

    def __init__(self, *, session: AsyncSession, run_id: uuid.UUID) -> None:
        self._session = session
        self._run_id = run_id
        self._seq = 0

    async def resync_seq(self) -> None:
        result = await self._session.execute(
            select(func.coalesce(func.max(RunEvent.seq), -1)).where(
                RunEvent.run_id == self._run_id
            )
        )
        self._seq = result.scalar_one() + 1

    async def record(
        self,
        event_type: RunEventType,
        *,
        node_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._session.add(
            RunEvent(
                run_id=self._run_id,
                seq=self._seq,
                event_type=event_type,
                node_name=node_name,
                payload=payload or {},
            )
        )
        await self._session.flush()
        self._seq += 1


async def _fail_run(
    *,
    session: AsyncSession,
    run: Run,
    recorder: RunEventRecorder,
    error: str,
    started: float,
) -> None:
    """Recover the transaction before persisting a terminal failure."""
    await session.rollback()
    # Rollback expires attributes even with expire_on_commit=False.
    await session.refresh(run)
    await recorder.resync_seq()
    run.sqlmodel_update(
        {
            "status": RunStatus.FAILED,
            "error": error[:4000],
            "finished_at": get_datetime_utc(),
            "duration_ms": max(1, int((time.perf_counter() - started) * 1000)),
        }
    )
    session.add(run)
    await recorder.record(RunEventType.RUN_FAILED, payload={"error": run.error})
    await session.commit()
    await session.refresh(run)


async def execute_run(
    *,
    session: AsyncSession,
    run: Run,
    snapshot: AgentSnapshot,
    initial_messages: list[BaseMessage],
) -> Run:
    """Execute an authorized, persisted run using its immutable snapshot.

    Phase 03 records only run_started/run_finished/run_failed. Phase 04 will
    collect node and model events through LangGraph streaming.
    """
    recorder = RunEventRecorder(session=session, run_id=run.id)
    started = time.perf_counter()
    try:
        run.sqlmodel_update(
            {"status": RunStatus.RUNNING, "started_at": get_datetime_utc()}
        )
        session.add(run)
        await recorder.record(RunEventType.RUN_STARTED, payload={"input": run.input})
        await session.commit()
        initial_state: AgentState = {
            "messages": initial_messages,
            "run_id": run.id,
            "agent_version_id": run.agent_version_id,
            "system_prompt": snapshot.system_prompt,
            "llm_model": snapshot.llm_model,
            "llm_settings": snapshot.llm_settings,
            "max_iterations": snapshot.max_iterations,
            "iteration": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        final_state = await asyncio.wait_for(
            compile_agent_graph().ainvoke(initial_state),
            timeout=snapshot.timeout_seconds,
        )
        run.sqlmodel_update(
            {
                "status": RunStatus.SUCCEEDED,
                "output": {"content": final_state["messages"][-1].content},
                "prompt_tokens": final_state["prompt_tokens"],
                "completion_tokens": final_state["completion_tokens"],
                "cost_usd": estimate_cost_usd(
                    model=snapshot.llm_model,
                    prompt_tokens=final_state["prompt_tokens"],
                    completion_tokens=final_state["completion_tokens"],
                ),
                "finished_at": get_datetime_utc(),
                "duration_ms": max(1, int((time.perf_counter() - started) * 1000)),
            }
        )
        session.add(run)
        await recorder.record(
            RunEventType.RUN_FINISHED,
            payload={
                "output": run.output,
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
            },
        )
        await session.commit()
    except TimeoutError as exc:
        error = f"Run exceeded timeout of {snapshot.timeout_seconds}s"
        await _fail_run(
            session=session, run=run, recorder=recorder, error=error, started=started
        )
        raise AgentTimeoutError(error) from exc
    except asyncio.CancelledError:
        await _fail_run(
            session=session,
            run=run,
            recorder=recorder,
            error="Run execution was cancelled",
            started=started,
        )
        raise
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        await _fail_run(
            session=session, run=run, recorder=recorder, error=error, started=started
        )
        if isinstance(exc, AgentError):
            raise
        raise AgentError(error) from exc
    await session.refresh(run)
    return run


async def _cancel_run(*, session: AsyncSession, run: Run) -> None:
    """Persist cancellation even while the response task is being cancelled."""
    with anyio.CancelScope(shield=True):
        await session.rollback()
        await session.refresh(run)
        if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            return
        recorder = RunEventRecorder(session=session, run_id=run.id)
        await recorder.resync_seq()
        now = get_datetime_utc()
        run.sqlmodel_update(
            {
                "status": RunStatus.CANCELLED,
                "finished_at": now,
                "duration_ms": max(
                    1, int((now - (run.started_at or now)).total_seconds() * 1000)
                ),
            }
        )
        session.add(run)
        await recorder.record(
            RunEventType.RUN_FINISHED, payload={"status": "cancelled"}
        )
        await session.commit()
        await session.refresh(run)


async def stream_run(
    *,
    session: AsyncSession,
    run: Run,
    snapshot: AgentSnapshot,
    context_messages: list[BaseMessage],
) -> AsyncGenerator[tuple[RunEventType, dict[str, Any]]]:
    """Stream graph events and atomically persist the reply and successful run."""
    recorder = RunEventRecorder(session=session, run_id=run.id)
    started = time.perf_counter()
    run_id = run.id
    try:
        run.sqlmodel_update(
            {"status": RunStatus.RUNNING, "started_at": get_datetime_utc()}
        )
        session.add(run)
        await recorder.record(RunEventType.RUN_STARTED, payload={"input": run.input})
        await session.commit()
        yield RunEventType.RUN_STARTED, {"run_id": str(run_id)}
        initial_state: AgentState = {
            "messages": context_messages,
            "run_id": run_id,
            "agent_version_id": run.agent_version_id,
            # The context builder already includes the prompt and summary.
            "system_prompt": "",
            "llm_model": snapshot.llm_model,
            "llm_settings": snapshot.llm_settings,
            "max_iterations": snapshot.max_iterations,
            "iteration": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        final_state: Any = None
        async with asyncio.timeout(snapshot.timeout_seconds):
            async for event in compile_agent_graph().astream_events(
                initial_state, version="v2"
            ):
                kind, name = event["event"], event["name"]
                if name in KNOWN_NODE_NAMES and kind in {
                    "on_chain_start",
                    "on_chain_end",
                }:
                    event_type = (
                        RunEventType.NODE_STARTED
                        if kind == "on_chain_start"
                        else RunEventType.NODE_FINISHED
                    )
                    payload: dict[str, Any] = {"node": name}
                    await recorder.record(event_type, node_name=name, payload=payload)
                    await session.commit()
                    yield event_type, payload
                elif kind == "on_chat_model_stream":
                    text = _as_text(event["data"]["chunk"].content)
                    if text:
                        yield RunEventType.MODEL_CHUNK, {"text": text}
                elif kind == "on_chain_end" and name == "LangGraph":
                    final_state = event["data"]["output"]
        if final_state is None:
            raise AgentError("Graph did not return a final state")
        reply = final_state["messages"][-1]
        run.sqlmodel_update(
            {
                "status": RunStatus.SUCCEEDED,
                "output": {"content": _as_text(reply.content)},
                "prompt_tokens": final_state["prompt_tokens"],
                "completion_tokens": final_state["completion_tokens"],
                "cost_usd": estimate_cost_usd(
                    model=snapshot.llm_model,
                    prompt_tokens=final_state["prompt_tokens"],
                    completion_tokens=final_state["completion_tokens"],
                ),
                "finished_at": get_datetime_utc(),
                "duration_ms": max(1, int((time.perf_counter() - started) * 1000)),
            }
        )
        message_id: uuid.UUID | None = None
        if run.conversation_id is not None:
            conversation = await session.get(
                Conversation, run.conversation_id, with_for_update=True
            )
            if conversation is None:
                raise AgentError("Conversation not found")
            seq = (
                await session.execute(
                    select(func.coalesce(func.max(ConvMessage.seq), 0)).where(
                        ConvMessage.conversation_id == conversation.id
                    )
                )
            ).scalar_one() + 1
            message = from_langchain_message(
                reply, conversation_id=conversation.id, seq=seq, run_id=run_id
            )
            message.token_count = run.completion_tokens
            session.add(message)
            message_id = message.id
            conversation.sqlmodel_update({"updated_at": get_datetime_utc()})
            session.add(conversation)
        session.add(run)
        payload = {
            "run_id": str(run_id),
            "message_id": str(message_id) if message_id else None,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "cost_usd": str(run.cost_usd) if run.cost_usd is not None else None,
        }
        await recorder.record(RunEventType.RUN_FINISHED, payload=payload)
        await session.commit()
        await session.refresh(run)
        yield RunEventType.RUN_FINISHED, payload
    except asyncio.CancelledError:
        await _cancel_run(session=session, run=run)
        raise
    except Exception as exc:
        error = (
            f"Run exceeded timeout of {snapshot.timeout_seconds}s"
            if isinstance(exc, TimeoutError)
            else str(exc) or type(exc).__name__
        )
        await _fail_run(
            session=session, run=run, recorder=recorder, error=error, started=started
        )
        yield RunEventType.RUN_FAILED, {"run_id": str(run_id), "error": run.error}
    finally:
        await _cancel_run(session=session, run=run)
