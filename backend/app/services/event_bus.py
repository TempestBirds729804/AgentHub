import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from redis.asyncio import Redis

from app.core.async_db import async_session_maker
from app.models import Run, RunEventType, RunStatus

EVENT_STREAM_MAXLEN = 1000
EVENT_STREAM_TTL_SECONDS = 3600
TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
TERMINAL_EVENT_TYPES = {"run_finished", "run_failed"}


def run_stream_key(run_id: uuid.UUID) -> str:
    return f"agenthub:run:{run_id}:events"


def cancel_key(run_id: uuid.UUID) -> str:
    return f"agenthub:run:{run_id}:cancel"


class RedisEventPublisher:
    """Forward events; durable trace remains in PostgreSQL."""

    def __init__(self, *, redis: Redis, run_id: uuid.UUID) -> None:
        self._redis = redis
        self._key = run_stream_key(run_id)

    async def publish(self, event_type: RunEventType, payload: dict[str, Any]) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(
                self._key,
                {"type": event_type.value, "payload": json.dumps(payload, default=str)},
                maxlen=EVENT_STREAM_MAXLEN,
                approximate=True,
            )
            # Also expire during execution: kill -9 cannot call close().
            pipe.expire(self._key, EVENT_STREAM_TTL_SECONDS)
            result = await pipe.execute()
            payload["event_id"] = result[0]

    async def close(self) -> None:
        await self._redis.expire(self._key, EVENT_STREAM_TTL_SECONDS)


async def terminal_run_payload(run_id: uuid.UUID) -> dict[str, Any] | None:
    async with async_session_maker() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return {"run_id": str(run_id), "status": "deleted"}
        if run.status in TERMINAL_STATUSES:
            return {
                "run_id": str(run_id),
                "status": run.status,
                "error": run.error,
                "output": run.output,
            }
        if run.status == RunStatus.WAITING_APPROVAL:
            return {"run_id": str(run_id), "status": run.status}
    return None


async def subscribe_run_events(
    *, redis: Redis, run_id: uuid.UUID, last_id: str = "0"
) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
    """Replay buffered events, then follow the worker until a terminal state."""
    while True:
        entries = await redis.xread(
            {run_stream_key(run_id): last_id}, count=50, block=5000
        )
        if not entries:
            terminal = await terminal_run_payload(run_id)
            if terminal is not None:
                yield (
                    "approval_requested"
                    if terminal.get("status") == RunStatus.WAITING_APPROVAL
                    else "run_finished",
                    terminal,
                )
                return
            continue
        for _key, messages in entries:
            for message_id, fields in messages:
                last_id = message_id
                payload = json.loads(fields["payload"])
                payload["event_id"] = message_id
                yield fields["type"], payload
                if fields["type"] in TERMINAL_EVENT_TYPES:
                    return
