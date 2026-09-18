import asyncio
import logging
import signal
import sys
from typing import Any

from arq import cron
from arq.worker import create_worker

from app.agent.tools.registry import close_tool_executors
from app.core.async_db import async_engine
from app.core.redis import close_redis
from app.worker.settings import (
    JOB_TIMEOUT_SECONDS,
    close_arq_pool,
    get_arq_redis_settings,
)
from app.worker.tasks import execute_run_task, resume_run_task

logger = logging.getLogger(__name__)


async def startup(_ctx: dict[str, Any]) -> None:
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    if not app_logger.hasHandlers():
        app_logger.addHandler(logging.StreamHandler())
    logger.info("Worker starting up")


async def shutdown(_ctx: dict[str, Any]) -> None:
    await close_tool_executors()
    await close_arq_pool()
    await close_redis()
    await async_engine.dispose()
    logger.info("Worker shut down")


class WorkerSettings:
    functions = [execute_run_task, resume_run_task]
    cron_jobs = [cron("app.worker.tasks.reap_stale_runs", minute=set(range(0, 60, 5)))]
    redis_settings = get_arq_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 5
    job_timeout = JOB_TIMEOUT_SECONDS
    max_tries = 2
    keep_result = 3600


async def run_local_worker() -> None:
    """Construct and close arq inside the Runner's Selector loop on Windows."""
    worker = create_worker(dict(vars(WorkerSettings)))
    try:
        await worker.async_run()
    finally:
        # Keep handle_signals=True: arq 0.28 close(False) uses Windows' missing SIGUSR1.
        worker.handle_sig(signal.SIGINT)
        await worker.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=factory) as runner:
            runner.run(run_local_worker())
    except KeyboardInterrupt, asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
