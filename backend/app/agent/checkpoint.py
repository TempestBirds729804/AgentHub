"""Persist execution state, independently of business conversation history.

The message table serves UI/history/context building; checkpoints serve recovery
only. Each Run has its own thread so conversation context is never appended twice.
Tools and MCP connections are rebuilt from the version, never serialized here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import settings


@asynccontextmanager
async def checkpointer_context() -> AsyncIterator[AsyncPostgresSaver]:
    conn_string = str(settings.DATABASE_URL).replace(
        "postgresql+psycopg://", "postgresql://"
    )
    async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        yield checkpointer


async def setup_checkpointer_once() -> None:
    """Create/migrate LangGraph tables once during application prestart."""
    async with checkpointer_context() as checkpointer:
        await checkpointer.setup()
