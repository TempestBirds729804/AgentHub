from typing import Any

from app.models.tool import ToolType


def tools_requiring_approval(
    *, tool_calls: list[dict[str, Any]], approval_required_names: set[str]
) -> list[dict[str, Any]]:
    """Select calls explicitly marked for human approval."""
    return [call for call in tool_calls if call["name"] in approval_required_names]


def describe_risk(tool_name: str, tool_type: ToolType) -> str:
    if tool_type == ToolType.HTTP:
        return f"'{tool_name}' performs an outbound HTTP request that may modify external state."
    if tool_type == ToolType.MCP:
        return f"'{tool_name}' invokes an external MCP server."
    return f"'{tool_name}' was marked as requiring approval."
