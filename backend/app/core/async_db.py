from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# The psycopg SQLAlchemy dialect supports both sync and async engines. Agent runtime
# code uses this engine and native ``await session.execute(...)`` calls; CRUD routes
# continue to use the synchronous engine and must not mix both sessions in a request.
async_engine: AsyncEngine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

async_session_maker = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_maker() as session:
        yield session
