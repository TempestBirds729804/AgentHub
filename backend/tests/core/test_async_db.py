import asyncio
import sys

import pytest
from sqlalchemy import text

from app.core.async_db import async_session_maker


@pytest.fixture(scope="session")
def event_loop_policy() -> object:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.mark.asyncio
async def test_async_engine_connects() -> None:
    async with async_session_maker() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
