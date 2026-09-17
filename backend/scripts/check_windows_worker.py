"""Probe arq/Redis/async PostgreSQL on Windows without running production jobs.

Run from backend: uv run --with arq==0.28.0 python scripts/check_windows_worker.py
Use --loop default to reproduce the Windows Proactor/psycopg failure.
"""

import argparse
import asyncio
import logging
import signal
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings, create_pool
from arq.constants import (
    in_progress_key_prefix,
    job_key_prefix,
    result_key_prefix,
    retry_key_prefix,
)
from arq.worker import Worker
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.async_db import async_engine, async_session_maker  # noqa: E402
from app.core.config import settings  # noqa: E402

logger = logging.getLogger(__name__)


async def database_probe(_ctx: dict[str, Any]) -> int:
    """Execute a real read-only database query inside an arq job."""
    async with async_session_maker() as session:
        return int((await session.execute(text("SELECT 1"))).scalar_one())


async def cancellation_probe(ctx: dict[str, Any]) -> None:
    """Keep one job active until the probe requests worker shutdown."""
    ctx["started"].set()
    try:
        await asyncio.Event().wait()
    finally:
        ctx["cancelled"].set()


async def run_probe() -> None:
    prefix = f"agenthub:windows-probe:{uuid.uuid4().hex}"
    queue_names = [f"{prefix}:database", f"{prefix}:cancel"]
    job_ids = [f"{prefix}:job:{i}" for i in range(2)]
    pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    workers: list[Worker] = []
    try:
        logger.info("Event loop: %s", type(asyncio.get_running_loop()).__name__)
        job = await pool.enqueue_job(
            "database_probe", _queue_name=queue_names[0], _job_id=job_ids[0]
        )
        assert job is not None
        worker = Worker(
            [database_probe],
            queue_name=queue_names[0],
            redis_settings=RedisSettings.from_dsn(settings.REDIS_URL),
            burst=True,
            poll_delay=0.05,
            job_timeout=5,
            keep_result=60,
        )
        workers.append(worker)
        await worker.async_run()
        assert await job.result(timeout=5) == 1
        assert worker.jobs_complete == 1 and worker.jobs_failed == 0
        await worker.close()
        workers.remove(worker)
        logger.info("Redis enqueue, arq job and async PostgreSQL SELECT 1 passed")

        started, cancelled = asyncio.Event(), asyncio.Event()
        await pool.enqueue_job(
            "cancellation_probe", _queue_name=queue_names[1], _job_id=job_ids[1]
        )
        worker = Worker(
            [cancellation_probe],
            queue_name=queue_names[1],
            redis_settings=RedisSettings.from_dsn(settings.REDIS_URL),
            poll_delay=0.05,
            job_timeout=10,
            retry_jobs=False,
            ctx={"started": started, "cancelled": cancelled},
        )
        workers.append(worker)
        running = asyncio.create_task(worker.async_run())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            worker.handle_sig(signal.SIGINT)
            with suppress(asyncio.CancelledError):
                await running
            await worker.close()
            workers.remove(worker)
            assert cancelled.is_set()
            assert not [task for task in worker.tasks.values() if not task.done()]
            logger.info("Programmatic SIGINT cancellation and worker.close passed")
        finally:
            if not running.done():
                running.cancel()
                with suppress(asyncio.CancelledError):
                    await running
    finally:
        for worker in workers:
            worker.handle_sig(signal.SIGINT)
            with suppress(asyncio.CancelledError):
                await worker.close()
        await async_engine.dispose()
        # Only remove keys belonging to this probe; never flush a shared Redis DB.
        keys = queue_names + [f"{q}:health-check" for q in queue_names]
        keys.extend(
            f"{key_prefix}{job_id}"
            for job_id in job_ids
            for key_prefix in (
                job_key_prefix,
                result_key_prefix,
                in_progress_key_prefix,
                retry_key_prefix,
            )
        )
        await pool.delete(*keys)
        await pool.aclose(close_connection_pool=True)


async def bounded_probe() -> None:
    async with asyncio.timeout(25):
        await run_probe()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", choices=("selector", "default"), default="selector")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    factory = asyncio.SelectorEventLoop if args.loop == "selector" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(bounded_probe())
