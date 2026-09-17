from redis.asyncio import Redis

from app.core.config import settings

_pool: Redis | None = None


def get_redis() -> Redis:
    """Return the process-local, decoded Redis connection pool."""
    global _pool
    if _pool is None:
        _pool = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=10,
        )
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
