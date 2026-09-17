import asyncio
import uuid

from fastapi.testclient import TestClient

from app.core.redis import get_redis
from app.services.rate_limit import (
    concurrent_runs_key,
    release_run_slot,
    try_acquire_run_slot,
)


def test_atomic_slots_and_idempotent_release(client: TestClient) -> None:
    async def check() -> None:
        user, runs = uuid.uuid4(), [uuid.uuid4() for _ in range(20)]
        redis = get_redis()
        results = await asyncio.gather(
            *(
                try_acquire_run_slot(redis=redis, user_id=user, run_id=run)
                for run in runs
            )
        )
        assert sum(results) == 3
        acquired = [run for run, ok in zip(runs, results, strict=True) if ok]
        assert await redis.ttl(concurrent_runs_key(user)) > 0
        assert await try_acquire_run_slot(redis=redis, user_id=user, run_id=acquired[0])
        assert int(await redis.get(concurrent_runs_key(user))) == 3
        for _ in range(2):
            await release_run_slot(redis=redis, user_id=user, run_id=acquired[0])
        assert int(await redis.get(concurrent_runs_key(user))) == 2
        assert await try_acquire_run_slot(
            redis=redis, user_id=user, run_id=uuid.uuid4()
        )

    assert client.portal is not None
    client.portal.call(check)
