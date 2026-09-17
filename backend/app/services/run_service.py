import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, cast

import anyio
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, func, select

from app.agent.checkpoint import checkpointer_context
from app.agent.exceptions import AgentError, AgentTimeoutError
from app.agent.graph import compile_agent_graph
from app.agent.memory import _as_text, build_context_messages, from_langchain_message
from app.agent.pricing import estimate_cost_usd
from app.agent.snapshot import AgentSnapshot
from app.agent.state import AgentState
from app.core.redis import get_redis
from app.models import (
    AgentVersion,
    Conversation,
    ConvMessage,
    MessageRole,
    Run,
    RunEvent,
    RunEventType,
    RunStatus,
    get_datetime_utc,
)
from app.services.event_bus import RedisEventPublisher, cancel_key
from app.services.tool_service import load_tools_for_version

KNOWN_NODE_NAMES = {"call_model", "tools"}
logger = logging.getLogger(__name__)


async def execute_run_with_checkpoint(
    *, session: AsyncSession, run: Run, publisher: RedisEventPublisher
) -> Run:
    """Execute/resume the latest durable state using freshly loaded tools."""
    run_id = run.id
    recorder = RunEventRecorder(session=session, run_id=run_id)
    await recorder.resync_seq()
    started = time.perf_counter()
    timeout_seconds: int | None = None

    async def emit(event_type: RunEventType, payload: dict[str, Any]) -> None:
        data = {"run_id": str(run_id), **payload}
        if event_type != RunEventType.MODEL_CHUNK:
            await recorder.record(
                event_type, node_name=payload.get("node"), payload=data
            )
            await session.commit()
        await publisher.publish(event_type, data)

    async with checkpointer_context() as checkpointer:
        graph = None
        config: RunnableConfig = {}

        async def save_checkpoint() -> dict[str, Any]:
            if graph is None:
                return {}
            state = await graph.aget_state(config)
            checkpoint_id = (
                (state.config or {}).get("configurable", {}).get("checkpoint_id")
            )
            if checkpoint_id:
                run.sqlmodel_update({"checkpoint_id": checkpoint_id})
                session.add(run)
                await session.commit()
            return dict(state.values)

        try:
            version = await session.get(AgentVersion, run.agent_version_id)
            if version is None:
                raise AgentError("Agent version not found")
            snapshot = AgentSnapshot.model_validate(version.snapshot)
            timeout_seconds = snapshot.timeout_seconds
            tools = await load_tools_for_version(
                session=session, agent_version_id=version.id
            )
            graph = compile_agent_graph(tools, checkpointer=checkpointer)
            # A fresh retry resets thread_id; retry_count makes its thread unique.
            thread_id = (
                run.thread_id
                if run.thread_id and run.thread_id.startswith(f"run-{run_id}")
                else f"run-{run_id}-{run.retry_count}"
            )
            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 2 * snapshot.max_iterations + 1,
            }
            saved = await graph.aget_state(config)
            # Always resume the latest committed checkpoint, even if kill -9
            # happened before run.checkpoint_id caught up with the saver.
            resuming = bool(saved.values)
            await session.refresh(run, with_for_update=True)
            if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return run
            run.sqlmodel_update(
                {
                    "thread_id": thread_id,
                    "status": RunStatus.RUNNING,
                    "started_at": get_datetime_utc(),
                    "finished_at": None,
                    "error": None,
                }
            )
            session.add(run)
            await session.commit()
            if resuming:
                logger.info("Resuming run %s from checkpoint %s", run_id, saved.config)
            await emit(RunEventType.RUN_STARTED, {"resumed": resuming})
            graph_input: AgentState | None = None
            if not resuming:
                messages: list[BaseMessage] = [
                    HumanMessage(content=run.input["message"])
                ]
                prompt = snapshot.system_prompt
                if run.conversation_id is not None:
                    conversation = await session.get(
                        Conversation, run.conversation_id, with_for_update=True
                    )
                    if conversation is None:
                        raise AgentError("Conversation not found")
                    history = list(
                        (
                            await session.execute(
                                select(ConvMessage)
                                .where(ConvMessage.conversation_id == conversation.id)
                                .order_by(col(ConvMessage.seq))
                            )
                        ).scalars()
                    )
                    own_message = next(
                        (
                            m
                            for m in history
                            if m.run_id == run_id and m.role == MessageRole.USER
                        ),
                        None,
                    )
                    if own_message is None:
                        own_message = ConvMessage(
                            conversation_id=conversation.id,
                            seq=history[-1].seq + 1 if history else 1,
                            role=MessageRole.USER,
                            content=run.input["message"],
                            run_id=run_id,
                        )
                        session.add(own_message)
                        history.append(own_message)
                    messages = build_context_messages(
                        history=[m for m in history if m.seq <= own_message.seq],
                        summary=conversation.summary,
                        system_prompt=prompt,
                    )
                    prompt = ""
                    await session.commit()
                graph_input = {
                    "messages": messages,
                    "run_id": run_id,
                    "agent_version_id": version.id,
                    "system_prompt": prompt,
                    "llm_model": snapshot.llm_model,
                    "llm_settings": snapshot.llm_settings,
                    "max_iterations": snapshot.max_iterations,
                    "iteration": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
            max_index = (
                await session.execute(
                    select(func.max(RunEvent.payload["index"].as_integer())).where(
                        RunEvent.run_id == run_id,
                        RunEvent.event_type == RunEventType.TOOL_CALLED,
                    )
                )
            ).scalar_one()
            next_index = (max_index + 1) if max_index is not None else 0
            calls: dict[str, tuple[int, float]] = {}
            async with asyncio.timeout(snapshot.timeout_seconds):
                # None is LangGraph's continuation input; supplying the original
                # messages here would start a new execution instead of resuming.
                async with aclosing(
                    cast(
                        AsyncGenerator[dict[str, Any]],
                        graph.astream_events(
                            graph_input, config=config, version="v2", durability="sync"
                        ),
                    )
                ) as stream:
                    async for event in stream:
                        kind, name = event["event"], event["name"]
                        if name in KNOWN_NODE_NAMES and kind in {
                            "on_chain_start",
                            "on_chain_end",
                        }:
                            await save_checkpoint()
                            await session.refresh(run)
                            if str(
                                run.status
                            ) == RunStatus.CANCELLED or await get_redis().exists(
                                cancel_key(run_id)
                            ):
                                raise RunCancelledError()
                            await emit(
                                RunEventType.NODE_STARTED
                                if kind == "on_chain_start"
                                else RunEventType.NODE_FINISHED,
                                {"node": name},
                            )
                        elif kind == "on_chat_model_stream":
                            chunk = _as_text(event["data"]["chunk"].content)
                            if chunk:
                                await emit(RunEventType.MODEL_CHUNK, {"text": chunk})
                        elif kind == "on_tool_start":
                            calls[str(event["run_id"])] = (
                                next_index,
                                time.perf_counter(),
                            )
                            await emit(
                                RunEventType.TOOL_CALLED,
                                {
                                    "tool": name,
                                    "args": event["data"].get("input"),
                                    "index": next_index,
                                },
                            )
                            next_index += 1
                        elif kind == "on_tool_end":
                            index, tool_started = calls[str(event["run_id"])]
                            result = event["data"].get("output")
                            artifact = (
                                result.artifact
                                if isinstance(result, ToolMessage)
                                and isinstance(result.artifact, dict)
                                else {}
                            )
                            content = (
                                _as_text(result.content)
                                if isinstance(result, BaseMessage)
                                else str(result)
                            )
                            await emit(
                                RunEventType.TOOL_RESULT,
                                {
                                    "tool": name,
                                    "index": index,
                                    "result": content[:2000],
                                    "ok": artifact.get(
                                        "ok",
                                        not isinstance(result, ToolMessage)
                                        or result.status != "error",
                                    ),
                                    "error": artifact.get("error"),
                                    "duration_ms": int(
                                        (time.perf_counter() - tool_started) * 1000
                                    ),
                                },
                            )
            final_state = await save_checkpoint()
            if not final_state:
                raise AgentError("Graph did not return a final state")
            await session.refresh(run, with_for_update=True)
            if str(run.status) == RunStatus.CANCELLED:
                raise RunCancelledError()
            if run.status != RunStatus.RUNNING:
                return run
            reply = final_state["messages"][-1]
            run.sqlmodel_update(
                {
                    "status": RunStatus.SUCCEEDED,
                    "output": _run_output(reply),
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
            if run.conversation_id is not None:
                conversation = await session.get(
                    Conversation, run.conversation_id, with_for_update=True
                )
                if conversation is not None:
                    seq = (
                        await session.execute(
                            select(func.coalesce(func.max(ConvMessage.seq), 0)).where(
                                ConvMessage.conversation_id == conversation.id
                            )
                        )
                    ).scalar_one() + 1
                    session.add(
                        from_langchain_message(
                            AIMessage(content=_as_text(reply.content)),
                            conversation_id=conversation.id,
                            seq=seq,
                            run_id=run_id,
                        )
                    )
                    conversation.sqlmodel_update({"updated_at": get_datetime_utc()})
                    session.add(conversation)
            session.add(run)
            await emit(
                RunEventType.RUN_FINISHED,
                {
                    "status": "succeeded",
                    "output": run.output,
                    "prompt_tokens": run.prompt_tokens,
                    "completion_tokens": run.completion_tokens,
                    "cost_usd": str(run.cost_usd) if run.cost_usd is not None else None,
                },
            )
        except RunCancelledError:
            await session.rollback()
            await session.refresh(run)
            await save_checkpoint()
            run.sqlmodel_update(
                {"status": RunStatus.CANCELLED, "finished_at": get_datetime_utc()}
            )
            session.add(run)
            await emit(
                RunEventType.RUN_FINISHED, {"status": "cancelled", "cancelled": True}
            )
        except asyncio.CancelledError:
            await session.rollback()
            await session.refresh(run)
            await save_checkpoint()
            raise
        except Exception as exc:
            logger.exception("Checkpoint execution failed for Run %s", run_id)
            await session.rollback()
            await session.refresh(run)
            await save_checkpoint()
            if run.status == RunStatus.CANCELLED:
                await emit(RunEventType.RUN_FINISHED, {"status": "cancelled"})
            elif run.status != RunStatus.SUCCEEDED:
                await _fail_run(
                    session=session,
                    run=run,
                    recorder=recorder,
                    error=f"Run exceeded timeout of {timeout_seconds}s"
                    if isinstance(exc, TimeoutError) and timeout_seconds is not None
                    else str(exc) or type(exc).__name__,
                    started=started,
                )
                await publisher.publish(
                    RunEventType.RUN_FAILED, {"run_id": str(run_id), "error": run.error}
                )
        await session.refresh(run)
    return run


class RunCancelledError(AgentError):
    """Cooperative user cancellation observed at a graph node boundary."""


def _run_output(reply: BaseMessage) -> dict[str, Any]:
    output: dict[str, Any] = {"content": _as_text(reply.content)}
    if isinstance(reply, AIMessage) and reply.tool_calls:
        output["truncated_by_max_iterations"] = True
    return output


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

    This blocking path records terminal lifecycle events. The streaming path
    additionally records node and tool events and emits model chunks.
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
        tools = await load_tools_for_version(
            session=session, agent_version_id=run.agent_version_id
        )
        final_state = await asyncio.wait_for(
            compile_agent_graph(tools).ainvoke(
                initial_state,
                config={"recursion_limit": 2 * snapshot.max_iterations + 1},
            ),
            timeout=snapshot.timeout_seconds,
        )
        run.sqlmodel_update(
            {
                "status": RunStatus.SUCCEEDED,
                "output": _run_output(final_state["messages"][-1]),
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
        tool_calls: dict[str, tuple[int, float]] = {}
        async with asyncio.timeout(snapshot.timeout_seconds):
            tools = await load_tools_for_version(
                session=session, agent_version_id=run.agent_version_id
            )
            async for event in compile_agent_graph(tools).astream_events(
                initial_state,
                version="v2",
                config={"recursion_limit": 2 * snapshot.max_iterations + 1},
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
                elif kind == "on_tool_start":
                    index = len(tool_calls)
                    tool_calls[str(event["run_id"])] = (index, time.perf_counter())
                    payload = {
                        "tool": name,
                        "args": event["data"].get("input"),
                        "index": index,
                    }
                    await recorder.record(
                        RunEventType.TOOL_CALLED, node_name="tools", payload=payload
                    )
                    await session.commit()
                    yield RunEventType.TOOL_CALLED, payload
                elif kind == "on_tool_end":
                    index, tool_started = tool_calls[str(event["run_id"])]
                    result = event["data"].get("output")
                    artifact = (
                        result.artifact
                        if isinstance(result, ToolMessage)
                        and isinstance(result.artifact, dict)
                        else {}
                    )
                    content = (
                        _as_text(result.content)
                        if isinstance(result, BaseMessage)
                        else str(result)
                    )
                    payload = {
                        "tool": name,
                        "index": index,
                        "result": content[:2000],
                        "ok": artifact.get(
                            "ok",
                            not isinstance(result, ToolMessage)
                            or result.status != "error",
                        ),
                        "error": artifact.get("error"),
                        "duration_ms": int((time.perf_counter() - tool_started) * 1000),
                    }
                    await recorder.record(
                        RunEventType.TOOL_RESULT, node_name="tools", payload=payload
                    )
                    await session.commit()
                    yield RunEventType.TOOL_RESULT, payload
                elif kind == "on_chain_end" and name == "LangGraph":
                    final_state = event["data"]["output"]
        if final_state is None:
            raise AgentError("Graph did not return a final state")
        reply = final_state["messages"][-1]
        run.sqlmodel_update(
            {
                "status": RunStatus.SUCCEEDED,
                "output": _run_output(reply),
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
            # Persist a complete final answer only; intermediate tool exchanges
            # remain in RunEvent. Never store unmatched tool_calls in history.
            stored_reply = (
                AIMessage(content=_as_text(reply.content))
                if isinstance(reply, AIMessage) and reply.tool_calls
                else reply
            )
            message = from_langchain_message(
                stored_reply, conversation_id=conversation.id, seq=seq, run_id=run_id
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
            "truncated_by_max_iterations": bool(
                run.output and run.output.get("truncated_by_max_iterations")
            ),
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
