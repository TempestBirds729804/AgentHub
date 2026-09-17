import asyncio
import time
from dataclasses import asdict
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool
from referencing import Registry

from app.agent.tools.base import ToolResult
from app.agent.tools.registry import get_executor
from app.models.tool import Tool


async def execute_tool(
    tool: Tool,
    *,
    arguments: dict[str, Any],
    config_override: dict[str, Any] | None = None,
) -> ToolResult:
    """Validate every invocation, including dictionary-backed LangChain schemas."""
    started = time.perf_counter()
    try:
        # An empty registry permits local references without fetching remote schemas.
        Draft202012Validator(tool.parameters_schema, registry=Registry()).validate(
            arguments
        )
        return await asyncio.wait_for(
            get_executor(tool.tool_type).execute(
                config={**tool.config, **(config_override or {})},
                arguments=arguments,
                timeout_seconds=tool.timeout_seconds,
            ),
            tool.timeout_seconds,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            content="",
            error=(str(exc) or f"Timed out after {tool.timeout_seconds}s")[:1000],
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def to_langchain_tool(
    tool: Tool, *, config_override: dict[str, Any] | None = None
) -> StructuredTool:
    async def run(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        try:
            result = await execute_tool(
                tool, arguments=kwargs, config_override=config_override
            )
            return (
                result.content
                if result.ok
                else f"Tool execution failed: {result.error}",
                asdict(result),
            )
        except Exception as exc:
            return f"Tool execution failed: {str(exc)[:1000]}", {
                "ok": False,
                "error": str(exc)[:1000],
            }

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.parameters_schema,
        coroutine=run,
        response_format="content_and_artifact",
    )
