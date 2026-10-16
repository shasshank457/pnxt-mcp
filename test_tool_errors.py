import pytest
from mcp.server.mcpserver.exceptions import ToolError


@pytest.mark.asyncio
async def test_invalid_order_status_has_clear_model_safe_message():
    import server

    tool = next(
        tool for tool in server.mcp._tool_manager.list_tools() if tool.name == "get_orders"
    )
    with pytest.raises(ToolError, match="Invalid order_status.*Valid values"):
        await tool.fn(order_status="NOT_A_STATUS")
