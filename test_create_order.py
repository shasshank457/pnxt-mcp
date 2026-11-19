from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.orders import CREATE_ORDER_SPEC

SELLER = "11111111-1111-4111-8111-111111111111"
CHANNEL = "22222222-2222-4222-8222-222222222222"
PRODUCT = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def valid_args():
    address = {"name": "Jane Doe", "phone": "12345", "address_line1": "1 Main St", "city": "Pune", "state_or_region": "MH", "postal_code": "411001", "country_code": "IN", "email": "jane@example.com"}
    return {"order_no": "ORD-1", "order_date": "2026-11-14T10:00:00Z", "seller_id": SELLER, "channel_id": CHANNEL, "billing_address": address, "shipping_address": address, "items": [{"name": "Widget", "quantity_ordered": 2, "item_price": 10.5, "product_id": PRODUCT, "channel_sku": "W-1"}], "currency": "INR", "payment_method": "CASH", "order_weight": 2, "weight_unit_code": "KG"}


def make_api():
    api = AsyncMock()
    api.async_post = AsyncMock(return_value={"data": {"id": "order-1", "orderStatus": "PENDING"}})
    return api


@pytest.mark.asyncio
async def test_create_order_preview_without_post_and_estimated_total():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CREATE_ORDER_SPEC, valid_args(), WriteContext("user-1", "tenant-1"))
    assert preview["status"] == "preview"
    assert preview["proposed_changes"]["estimated_total"] == 21
    assert "Payment is not processed" in preview["warnings"][0]
    api.async_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_order_commit_exact_payload_and_backend_response():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CREATE_ORDER_SPEC, valid_args(), context)
        result = await framework.invoke(CREATE_ORDER_SPEC, {**valid_args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert result["result"] == {"data": {"id": "order-1", "orderStatus": "PENDING"}}
    payload = {"orderNo": "ORD-1", "orderDate": "2026-11-14T10:00:00Z", "sellerId": SELLER, "channelId": CHANNEL, "currency": "INR", "paymentMethod": "CASH", "orderWeight": 2, "weightUnitCode": "KG", "billingAddress": {"name": "Jane Doe", "phone": "12345", "addressLine1": "1 Main St", "city": "Pune", "stateOrRegion": "MH", "postalCode": "411001", "countryCode": "IN", "email": "jane@example.com"}, "shippingAddress": {"name": "Jane Doe", "phone": "12345", "addressLine1": "1 Main St", "city": "Pune", "stateOrRegion": "MH", "postalCode": "411001", "countryCode": "IN", "email": "jane@example.com"}, "orderItems": [{"name": "Widget", "quantityOrdered": 2, "itemPrice": 10.5, "channelSku": "W-1", "productId": PRODUCT}]}
    api.async_post.assert_awaited_once_with("/commerce/orders", payload, retry=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"order_no": "", "order_date": "2026-01-01", "seller_id": SELLER, "channel_id": CHANNEL, "billing_address": {}, "shipping_address": {}, "items": []},
    {**valid_args(), "seller_id": "bad"}, {**valid_args(), "order_date": "bad-date"}, {**valid_args(), "items": []},
    {**valid_args(), "items": [{"name": "x", "quantity_ordered": 0, "item_price": 1}]}, {**valid_args(), "items": [{"name": "x", "quantity_ordered": 1, "item_price": -1}]},
    {**valid_args(), "items": [{"name": "x", "quantity_ordered": float("nan"), "item_price": 1}]}, {**valid_args(), "order_weight": float("inf")},
    {**valid_args(), "currency": "US"}, {**valid_args(), "weight_unit_code": "BAD"}, {**valid_args(), "billing_address": {"name": "x"}},
])
async def test_create_order_validation(bad):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(CREATE_ORDER_SPEC, bad, WriteContext("user-1", "tenant-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenantId", "orderStatus", "orderTotal", "payment_state", "fulfillment_state"])
async def test_create_order_rejects_forbidden_fields(field):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(CREATE_ORDER_SPEC, {**valid_args(), field: "bad"}, WriteContext("user-1", "tenant-1"))


@pytest.mark.asyncio
async def test_create_order_confirmation_scope_reuse_and_idempotency():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CREATE_ORDER_SPEC, valid_args(), context)
        commit = {**valid_args(), "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "same"}
        first = await framework.invoke(CREATE_ORDER_SPEC, commit, context)
        second = await framework.invoke(CREATE_ORDER_SPEC, commit, context)
        assert second == {**first, "replayed": True}
        with pytest.raises(WriteError) as replay:
            await framework.invoke(CREATE_ORDER_SPEC, {**valid_args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
        with pytest.raises(WriteError):
            await framework.invoke(CREATE_ORDER_SPEC, {**valid_args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, WriteContext("user-2", "tenant-1"))
    assert replay.value.code == "TOKEN_USED"
    api.async_post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, "VALIDATION_FAILED"), (404, "NOT_FOUND"), (409, "CONFLICT"), (401, "SIGNED_OUT")])
async def test_create_order_backend_errors(status, code):
    api = make_api()
    api.async_post = AsyncMock(side_effect=httpx.HTTPStatusError("backend", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(status)))
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CREATE_ORDER_SPEC, valid_args(), context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(CREATE_ORDER_SPEC, {**valid_args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code
