from app.agent.tools.base import ToolExecutor
from app.agent.tools.function import FunctionToolExecutor
from app.agent.tools.http import HttpToolExecutor
from app.agent.tools.mcp import McpToolExecutor
from app.models.tool import ToolType

_EXECUTORS: dict[ToolType, ToolExecutor] = {
    ToolType.FUNCTION: FunctionToolExecutor(),
    ToolType.HTTP: HttpToolExecutor(),
    ToolType.MCP: McpToolExecutor(),
}


def get_executor(tool_type: ToolType) -> ToolExecutor:
    return _EXECUTORS[tool_type]


async def close_tool_executors() -> None:
    executor = _EXECUTORS[ToolType.MCP]
    assert isinstance(executor, McpToolExecutor)
    await executor.aclose()
