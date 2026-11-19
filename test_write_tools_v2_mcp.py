from unittest.mock import AsyncMock, patch

import pytest

import server
from services import write_tools
from services.write_tools import WriteContext, WriteToolFramework
from tools.write_tools_v2 import (
    ADJUST_STOCK_SPEC, BULK_CHANGE_ORDER_STATUS_SPEC, CANCEL_ORDER_SPEC,
    CREATE_RETURN_REQUEST_SPEC, EDIT_ORDER_TAGS_SPEC, UPDATE_SHIPPING_ADDRESS_SPEC,
)
from services.write_tools import register_write_tool

ORDER_ID = "11111111-1111-4111-8111-111111111111"

@pytest.fixture
def raw_tool(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())
    monkeypatch.setattr(write_tools, "_handler_context", lambda: WriteContext("u", "t"))
    return lambda spec: register_write_tool(server.mcp, spec, WriteToolFramework(secret="x" * 32))

def _api():
    api = AsyncMock()
    api.async_get = AsyncMock(side_effect=[
        {"data": {"items": [{"id": ORDER_ID}]}},
        {"data": {"id": ORDER_ID, "orderNo": "#TEST1613", "version": 4, "orderStatus": "CONFIRMED", "tags": ["VIP"]}},
    ])
    api.async_request = AsyncMock(return_value={"data": {"email": "alice@example.com", "phone": "9876543210", "tenantId": "hidden", "addressLine1": "hidden", "productName": "Widget"}})
    return api

@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"order_reference": "#TEST1613"},
    {"order_id": ORDER_ID},
    {"order_reference": "#TEST1613", "order_id": ORDER_ID},
    {"order_reference": "#TEST1613", "remove": []},
    {"order_reference": "#TEST1613", "remove": None},
])
async def test_registered_edit_tags_accepts_raw_optional_arguments(raw_tool, args):
    api = _api()
    with patch("tools.write_tools_v2.get_api", return_value=api):
        raw_tool(EDIT_ORDER_TAGS_SPEC)
        result = await server.mcp.call_tool("edit_order_tags", args)
    assert result is not None
    assert not (isinstance(result, dict) and result.get("isError"))

@pytest.mark.asyncio
async def test_registered_edit_tags_neither_is_invalid_input(raw_tool):
    with patch("tools.write_tools_v2.get_api", return_value=_api()):
        raw_tool(EDIT_ORDER_TAGS_SPEC)
        result = await server.mcp.call_tool("edit_order_tags", {})
    assert "INVALID_INPUT" in str(result)

@pytest.mark.asyncio
async def test_registered_edit_tags_mismatching_ids_is_invalid_input(raw_tool):
    api = _api()
    api.async_get.side_effect = [
        {"data": {"id": ORDER_ID, "orderNo": "#TEST1613", "version": 4, "tags": []}},
        {"data": {"items": [{"id": "22222222-2222-4222-8222-222222222222"}]}},
    ]
    with patch("tools.write_tools_v2.get_api", return_value=api):
        raw_tool(EDIT_ORDER_TAGS_SPEC)
        result = await server.mcp.call_tool("edit_order_tags", {"order_id": ORDER_ID, "order_reference": "#OTHER"})
    assert "INVALID_INPUT" in str(result)

@pytest.mark.asyncio
@pytest.mark.parametrize("spec,args", [
    (CANCEL_ORDER_SPEC, {"order_reference": "#TEST1613", "reason": "duplicate"}),
    (BULK_CHANGE_ORDER_STATUS_SPEC, {"order_references": ["#TEST1613"], "order_status": "SHIPPED"}),
    (UPDATE_SHIPPING_ADDRESS_SPEC, {"order_reference": "#TEST1613", "city": "Pune"}),
])
async def test_registered_new_order_tools_accept_absent_optionals(raw_tool, spec, args):
    api = _api()
    if spec is BULK_CHANGE_ORDER_STATUS_SPEC:
        api.async_get.side_effect = [
            {"data": {"CONFIRMED": ["SHIPPED"]}},
            {"data": {"items": [{"id": ORDER_ID}]}},
            {"data": {"id": ORDER_ID, "orderNo": "#TEST1613", "version": 4, "orderStatus": "CONFIRMED", "tags": []}},
        ]
    with patch("tools.write_tools_v2.get_api", return_value=api):
        raw_tool(spec)
        result = await server.mcp.call_tool(spec.name, args)
    assert result is not None

@pytest.mark.asyncio
async def test_capability_tools_fail_closed_through_registered_layer(raw_tool):
    for spec, args in [
        (ADJUST_STOCK_SPEC, {"sku": "SKU", "warehouse_id": "W", "quantity_change": -1, "reason": "LOST"}),
        (CREATE_RETURN_REQUEST_SPEC, {"items": [{"sku": "SKU", "quantity": 1}], "reason": "damaged", "order_reference": "#TEST1613"}),
    ]:
        raw_tool(spec)
        result = await server.mcp.call_tool(spec.name, args)
        assert "CAPABILITY_MISSING" in str(result)

