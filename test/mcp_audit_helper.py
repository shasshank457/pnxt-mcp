"""Direct invocation helpers for registered MCP tool handlers."""

from typing import Any


async def call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
    """Invoke a registered handler directly, bypassing MCP transport encoding."""
    from server import mcp

    tool = mcp._tool_manager._tools.get(name)
    if tool is None:
        raise KeyError(f"Unregistered MCP tool: {name}")
    result = tool.fn(**(args or {}))
    if hasattr(result, "__await__"):
        return await result
    return result
