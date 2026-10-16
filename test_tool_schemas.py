import pytest

import server
from tools.orders import get_orders
from tools.products import get_products
from tools.returns import search_returns
from tools.shipments import search_shipments


def _tool(name):
    return next(tool for tool in server.mcp._tool_manager.list_tools() if tool.name == name)


def _property(tool_name, parameter):
    return _tool(tool_name).parameters["properties"][parameter]


def test_status_parameters_publish_shared_enum_values():
    assert _property("get_orders", "order_status")["enum"]
    assert _property("search_returns", "return_status")["enum"] == [
        "RETURN_REQUESTED",
        "RETURN_RECEIVED",
        "PARTIALLY_RETURNED",
        "RETURNED",
        "REFUNDED",
        "PARTIALLY_REFUNDED",
    ]
    assert _property("search_products", "status")["enum"] == [
        "ACTIVE",
        "DRAFT",
        "ARCHIVED",
    ]
    assert _property("search_shipments", "ship_status")["enum"] == [
        "READY_FOR_PICKUP",
        "IN_TRANSIT",
        "SHIPPED",
        "DELIVERED",
        "CREATED",
    ]


def test_every_tool_and_parameter_has_description():
    for tool in server.mcp._tool_manager.list_tools():
        assert len((tool.description or "").split()) >= 10, tool.name
        for name, schema in tool.parameters.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name}"


@pytest.mark.asyncio
async def test_invalid_status_values_are_explicit():
    with pytest.raises(ValueError, match="Invalid order_status"):
        await get_orders(order_status="NOT_A_STATUS")
    with pytest.raises(ValueError, match="Invalid return_status"):
        await search_returns(return_status="NOT_A_STATUS")
    with pytest.raises(ValueError, match="Invalid status"):
        await get_products(status="NOT_A_STATUS")
    with pytest.raises(ValueError, match="Invalid ship_status"):
        await search_shipments(ship_status="NOT_A_STATUS")


@pytest.mark.asyncio
async def test_malformed_order_dates_are_explicit():
    with pytest.raises(ValueError, match="start_date must use YYYY-MM-DD format"):
        await get_orders(start_date="2026-02-30")
