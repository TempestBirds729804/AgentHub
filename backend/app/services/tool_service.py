import uuid

from langchain_core.tools import StructuredTool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.agent.tools.adapter import to_langchain_tool
from app.models import AgentToolBinding, Tool


async def load_tools_for_version(
    *, session: AsyncSession, agent_version_id: uuid.UUID
) -> list[StructuredTool]:
    result = await session.execute(
        select(Tool, AgentToolBinding.config_override)
        .join(AgentToolBinding, col(AgentToolBinding.tool_id) == Tool.id)
        .where(AgentToolBinding.agent_version_id == agent_version_id, Tool.is_active)
    )
    return [
        to_langchain_tool(tool, config_override=override)
        for tool, override in result.all()
    ]
