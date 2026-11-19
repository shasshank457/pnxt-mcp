import httpx
import pytest
from unittest.mock import AsyncMock, patch

from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.write_tools_v2 import (
    ADJUST_STOCK_SPEC, BULK_CHANGE_ORDER_STATUS_SPEC, CANCEL_ORDER_SPEC,
    CREATE_RETURN_REQUEST_SPEC, EDIT_ORDER_TAGS_SPEC, UPDATE_SHIPPING_ADDRESS_SPEC,
)

ORDER_ID = "11111111-1111-1111-1111-111111111111"

def order(**changes):
    value = {"id": ORDER_ID, "orderNo": "#TEST1613", "version": 3,
             "orderStatus": "CONFIRMED", "paymentMethod": "PREPAID",
             "shippingAddress": {"name": "Alice", "phone": "9876543210", "city": "Pune"},
             "tags": ["VIP", "Fragile"], "shipments": []}
    value.update(changes); return {"data": value}

@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr("services.write_tools.PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr("services.write_tools.PNXT_WRITE_TOOLS_ALLOWLIST", ())
    return WriteContext("u", "t")

@pytest.mark.asyncio
async def test_cancel_preview_no_write_and_requires_token(enabled):
    api = AsyncMock(); api.async_get = AsyncMock(return_value=order()); api.async_request = AsyncMock()
    with patch("tools.write_tools_v2.get_api", return_value=api):
        framework = WriteToolFramework(secret="x" * 32)
        preview = await framework.invoke(CANCEL_ORDER_SPEC, {"order_id": ORDER_ID, "reason": "duplicate"}, enabled)
        assert preview["status"] == "preview"; api.async_request.assert_not_called()
        with pytest.raises(WriteError, match="confirmation_token"):
            await framework.invoke(CANCEL_ORDER_SPEC, {"order_id": ORDER_ID, "reason": "duplicate", "confirm": True}, enabled)

@pytest.mark.asyncio
async def test_cancel_shipped_and_address_awb_are_blocked(enabled):
    api = AsyncMock(); api.async_get = AsyncMock(return_value=order(orderStatus="SHIPPED"))
    with patch("tools.write_tools_v2.get_api", return_value=api):
        with pytest.raises(WriteError):
            await WriteToolFramework(secret="x" * 32).invoke(CANCEL_ORDER_SPEC, {"order_id": ORDER_ID, "reason": "late"}, enabled)
    api.async_get = AsyncMock(return_value=order(shipments=[{"awbNo": "AWB1"}]))
    with patch("tools.write_tools_v2.get_api", return_value=api):
        with pytest.raises(WriteError, match="AWB"):
            await WriteToolFramework(secret="x" * 32).invoke(UPDATE_SHIPPING_ADDRESS_SPEC, {"order_id": ORDER_ID, "city": "Delhi"}, enabled)

@pytest.mark.asyncio
async def test_tags_no_change_and_idempotency_replay_is_masked(enabled):
    api = AsyncMock(); api.async_get = AsyncMock(return_value=order()); api.async_request = AsyncMock(return_value={"email":"alice@example.com","phone":"9876543210","tenantId":"secret","addressLine1":"hidden","productName":"Widget"})
    with patch("tools.write_tools_v2.get_api", return_value=api):
        framework = WriteToolFramework(secret="x" * 32)
        preview = await framework.invoke(EDIT_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "add": [], "remove": []}, enabled)
        assert "NO_CHANGE" in preview["warnings"]
        args={"order_id":ORDER_ID,"add":["New"],"remove":[],"idempotency_key":"k"}
        preview=await framework.invoke(EDIT_ORDER_TAGS_SPEC,args,enabled)
        commit={**args,"confirm":True,"confirmation_token":preview["confirmation_token"]}
        await framework.invoke(EDIT_ORDER_TAGS_SPEC,commit,enabled)
        replay=await framework.invoke(EDIT_ORDER_TAGS_SPEC,commit,enabled)
        assert replay["replayed"] is True and replay["result"]["email"] == "a***@example.com" and replay["result"]["phone"] == "******3210"
        assert api.async_request.call_count == 1

@pytest.mark.asyncio
async def test_bulk_split_and_capability_stubs_make_no_write(enabled):
    api=AsyncMock(); api.async_get=AsyncMock(side_effect=[{"data":{"CONFIRMED":["SHIPPED"]}}, {"data":{"items":[{"id":ORDER_ID}]}}, order()]); api.async_request=AsyncMock()
    with patch("tools.write_tools_v2.get_api", return_value=api):
        preview=await WriteToolFramework(secret="x"*32).invoke(BULK_CHANGE_ORDER_STATUS_SPEC,{"order_references":["#TEST1613","#TEST1613"],"order_status":"SHIPPED"},enabled)
        assert len(preview["proposed_changes"]["ready"]) == 1 and not api.async_request.called
    for spec,args in [(ADJUST_STOCK_SPEC,{"sku":"SKU","warehouse_id":"W","quantity_change":-1,"reason":"LOST"}), (CREATE_RETURN_REQUEST_SPEC,{"items":[],"reason":"damaged","order_id":ORDER_ID})]:
        with pytest.raises(WriteError) as exc: await WriteToolFramework(secret="x"*32).invoke(spec,args,enabled)
        assert exc.value.code == "CAPABILITY_MISSING"

@pytest.mark.asyncio
async def test_backend_401_maps_to_signed_out(enabled):
    api=AsyncMock(); api.async_get=AsyncMock(return_value=order()); api.async_request=AsyncMock(side_effect=httpx.HTTPStatusError("x",request=httpx.Request("PUT","https://x"),response=httpx.Response(401)))
    with patch("tools.write_tools_v2.get_api",return_value=api):
        framework=WriteToolFramework(secret="x"*32); preview=await framework.invoke(CANCEL_ORDER_SPEC,{"order_id":ORDER_ID,"reason":"duplicate"},enabled)
        with pytest.raises(WriteError) as exc: await framework.invoke(CANCEL_ORDER_SPEC,{"order_id":ORDER_ID,"reason":"duplicate","confirm":True,"confirmation_token":preview["confirmation_token"]},enabled)
        assert exc.value.code == "SIGNED_OUT"

