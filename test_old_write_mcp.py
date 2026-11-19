from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

import server
from services import write_tools
from services.write_tools import WriteContext, WriteToolFramework, register_write_tool
from tools.orders import CREATE_ORDER_SPEC
from tools.products import UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, UPDATE_PRODUCT_SPEC

PRODUCT_ID = "11111111-1111-4111-8111-111111111111"
CHANNEL_ID = "22222222-2222-4222-8222-222222222222"
TENANT = "tenant-1"

@pytest.fixture
def setup_tools(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())
    monkeypatch.setattr(write_tools, "_handler_context", lambda: WriteContext("u", TENANT))
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    specs = (UPDATE_PRODUCT_SPEC, UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, CREATE_ORDER_SPEC)
    for spec in specs:
        register_write_tool(server.mcp, spec, WriteToolFramework(secret="x" * 32))
    yield
    for spec in specs:
        if spec.name not in names:
            server.mcp.remove_tool(spec.name)

@pytest.mark.asyncio
async def test_update_product_raw_only_subtitle_does_not_validate_or_send_name(setup_tools):
    api = AsyncMock()
    api.async_get = AsyncMock(return_value={"data": {"id": PRODUCT_ID, "sku": "SKU", "tenantId": TENANT}})
    with patch("tools.products.get_api", return_value=api):
        result = await server.mcp.call_tool("update_product", {"product_id": PRODUCT_ID, "subtitle": "New"})
    assert "preview" in str(result)
    assert "name cannot be null" not in str(result)

@pytest.mark.asyncio
async def test_listing_raw_only_channel_buffer_and_explicit_null_inherit(setup_tools):
    api = AsyncMock()
    api.async_get = AsyncMock(side_effect=[
        {"data": {"id": PRODUCT_ID, "tenantId": TENANT}},
        {"data": {"id": "listing-1", "tenantId": TENANT, "productId": PRODUCT_ID, "channelId": CHANNEL_ID}},
    ])
    with patch("tools.products.get_api", return_value=api):
        result = await server.mcp.call_tool("update_product_channel_listing", {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "channel_buffer": 1})
    assert "channelBuffer" in str(result)
    assert "allocation_mode cannot be null" not in str(result)

    api.async_get.side_effect = [
        {"data": {"id": PRODUCT_ID, "tenantId": TENANT}},
        {"data": {"id": "listing-1", "tenantId": TENANT, "productId": PRODUCT_ID, "channelId": CHANNEL_ID}},
    ]
    with patch("tools.products.get_api", return_value=api):
        result = await server.mcp.call_tool("update_product_channel_listing", {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": None})
    assert "allocation" in str(result)

@pytest.mark.asyncio
async def test_create_order_raw_minimal_address_and_items(setup_tools):
    address = {"name": "Jane", "phone": "12345", "addressLine1": "1 Main", "city": "Pune", "stateOrRegion": "MH", "postalCode": "411001", "countryCode": "IN"}
    args = {"order_no": "RAW-1", "order_date": "2026-11-19T10:00:00Z", "seller_id": "33333333-3333-4333-8333-333333333333", "channel_id": CHANNEL_ID, "billing_address": address, "shipping_address": address, "items": [{"name": "Widget", "quantityOrdered": 1, "itemPrice": 10}]}
    with patch("tools.orders.get_api", return_value=AsyncMock()) as api:
        result = await server.mcp.call_tool("create_order", args)
    assert "preview" in str(result)
    api.async_post.assert_not_called()

@pytest.mark.asyncio
async def test_create_order_bad_address_names_fields(setup_tools):
    address = {"name": "Jane", "postalCode": 411001, "countryCode": "IN", "unexpected": "x"}
    args = {"order_no": "RAW-2", "order_date": "2026-11-19T10:00:00Z", "seller_id": "33333333-3333-4333-8333-333333333333", "channel_id": CHANNEL_ID, "billing_address": address, "shipping_address": address, "items": [{"name": "Widget", "quantityOrdered": 1, "itemPrice": 10}]}
    result = await server.mcp.call_tool("create_order", args)
    text = str(result)
    assert "missing" in text and "unexpected" in text and "phone" in text

def test_no_generic_parameter_descriptions_remain():
    for tool in server.mcp._tool_manager.list_tools():
        for schema in tool.parameters.get("properties", {}).values():
            assert "Optional text filter; matching behavior depends on the tool." not in (schema.get("description") or "")

