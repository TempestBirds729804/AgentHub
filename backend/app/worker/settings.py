from arq.connections import ArqRedis, RedisSettings, create_pool

from app.core.config import settings

JOB_TIMEOUT_SECONDS = 900
STALE_RUN_THRESHOLD_SECONDS = 1200
_arq_pool: ArqRedis | None = None


def get_arq_redis_settings() -> RedisSettings:
    config = RedisSettings.from_dsn(settings.REDIS_URL)
    config.conn_timeout = 3
    config.conn_retries = 0
    return config


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(get_arq_redis_settings())
    return _arq_pool


async def close_arq_pool() -> None:
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.aclose(close_connection_pool=True)
        _arq_pool = None
