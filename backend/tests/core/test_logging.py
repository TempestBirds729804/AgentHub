import asyncio
import logging

import pytest

from app.core.logging import ContextFilter, run_id_var, setup_logging, user_id_var


def test_context_filter_defaults_and_reset() -> None:
    record = logging.LogRecord("test", logging.INFO, "", 1, "message", (), None)
    ContextFilter().filter(record)
    assert record.run_id == record.user_id == "-"
    token = run_id_var.set("run-a")
    user = user_id_var.set("owner-a")
    try:
        ContextFilter().filter(record)
        assert record.run_id == "run-a" and record.user_id == "owner-a"
    finally:
        run_id_var.reset(token)
        user_id_var.reset(user)
    setup_logging()
    setup_logging()
    assert sum(h.name == "agenthub-context" for h in logging.getLogger().handlers) == 1


@pytest.mark.asyncio
async def test_context_isolation() -> None:
    async def task(name: str) -> str | None:
        token = run_id_var.set(name)
        try:
            await asyncio.sleep(0.01)
            return run_id_var.get()
        finally:
            run_id_var.reset(token)

    assert await asyncio.gather(task("a"), task("b")) == ["a", "b"]
    assert run_id_var.get() is None
