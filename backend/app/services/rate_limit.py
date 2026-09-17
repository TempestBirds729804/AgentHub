import uuid

from redis.asyncio import Redis

from app.core.config import settings

SLOT_TTL_SECONDS = 3600


def concurrent_runs_key(user_id: uuid.UUID) -> str:
    return f"agenthub:user:{user_id}:running"


def run_slot_key(run_id: uuid.UUID) -> str:
    return f"agenthub:run:{run_id}:slot"


async def try_acquire_run_slot(
    *, redis: Redis, user_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    """Atomically reserve a slot once per Run, including crash redeliveries."""
    # redis-py 5's shared eval annotation includes synchronous return types.
    result = await redis.execute_command(  # type: ignore[no-untyped-call]
        "EVAL",
        """
        if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
        local count = tonumber(redis.call('GET', KEYS[1]) or '0')
        if count >= tonumber(ARGV[1]) then return 0 end
        redis.call('INCR', KEYS[1])
        redis.call('EXPIRE', KEYS[1], ARGV[2])
        redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
        return 1
        """,
        2,
        concurrent_runs_key(user_id),
        run_slot_key(run_id),
        settings.AGENT_MAX_CONCURRENT_RUNS_PER_USER,
        SLOT_TTL_SECONDS,
    )
    return bool(result)


async def release_run_slot(
    *, redis: Redis, user_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """Duplicate cancellation, reaping and worker cleanup cannot double-release."""
    await redis.execute_command(  # type: ignore[no-untyped-call]
        "EVAL",
        """
        if redis.call('DEL', KEYS[2]) == 0 then return 0 end
        local count = tonumber(redis.call('GET', KEYS[1]) or '0')
        if count > 1 then
            redis.call('DECR', KEYS[1])
        else
            redis.call('DEL', KEYS[1])
        end
        return 1
        """,
        2,
        concurrent_runs_key(user_id),
        run_slot_key(run_id),
    )
