from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.products import UPDATE_PRODUCT_CHANNEL_LISTING_SPEC

PRODUCT_ID = "11111111-1111-4111-8111-111111111111"
CHANNEL_ID = "22222222-2222-4222-8222-222222222222"
LISTING_ID = "33333333-3333-4333-8333-333333333333"
TENANT = "tenant-1"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def make_api(listing=None):
    api = AsyncMock()
    listing = listing or {"id": LISTING_ID, "tenantId": TENANT, "productId": PRODUCT_ID, "channelId": CHANNEL_ID, "externalId": "EXT-1"}
    async def async_get(endpoint, **kwargs):
        if endpoint.endswith("/verify"):
            return {"data": listing}
        return {"data": {"id": PRODUCT_ID, "tenantId": TENANT}}
    api.async_get = AsyncMock(side_effect=async_get)
    api.async_request = AsyncMock(return_value={"data": {"id": LISTING_ID, "allocation": None}})
    return api


def args():
    return {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "external_id": "EXT-1", "allocation_mode": "PERCENT", "allocation": None, "allocation_fixed": 2.5, "channel_buffer": 1, "min_publish_qty": None, "max_publish_qty": 10, "zero_quantity_behaviour": "UNPUBLISH"}


@pytest.mark.asyncio
async def test_listing_preview_verifies_and_does_not_put():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, args(), WriteContext("user-1", TENANT))
    assert preview["status"] == "preview"
    assert preview["proposed_changes"]["selected_listing_id"] == LISTING_ID
    assert preview["proposed_changes"]["allocation"] is None
    api.async_request.assert_not_awaited()
    assert api.async_get.await_args_list[1].kwargs["params"] == {"externalId": "EXT-1"}


@pytest.mark.asyncio
async def test_listing_commit_reverifies_and_preserves_nulls_without_external_id_body():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, args(), context)
        committed = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {**args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert committed["result"] == {"data": {"id": LISTING_ID, "allocation": None}}
    api.async_request.assert_awaited_once_with("PUT", f"/commerce/products/{PRODUCT_ID}/channels/{CHANNEL_ID}/settings", json={"allocationMode": "PERCENT", "allocation": None, "allocationFixed": 2.5, "channelBuffer": 1, "minPublishQty": None, "maxPublishQty": 10, "zeroQuantityBehaviour": "UNPUBLISH"}, retry=False)
    assert "externalId" not in api.async_request.await_args.kwargs["json"]
    assert api.async_get.await_count == 4


def test_listing_schema_excludes_price_and_mrp():
    assert "price" not in UPDATE_PRODUCT_CHANNEL_LISTING_SPEC.input_schema["properties"]
    assert "mrp" not in UPDATE_PRODUCT_CHANNEL_LISTING_SPEC.input_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"product_id": "bad", "channel_id": CHANNEL_ID, "allocation": 1}, {"product_id": PRODUCT_ID, "channel_id": "bad", "allocation": 1},
    {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID}, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": -1},
    {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 101}, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": float("nan")},
    {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation_fixed": -1}, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "channel_buffer": float("inf")},
    {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "min_publish_qty": 1.5}, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation_mode": "BAD", "allocation": 1},
])
async def test_listing_validation(bad):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, bad, WriteContext("user-1", TENANT))


@pytest.mark.asyncio
async def test_listing_tenant_and_relationship_mismatch_blocks_put():
    for listing in [
        {"id": LISTING_ID, "productId": PRODUCT_ID, "channelId": CHANNEL_ID},
        {"id": LISTING_ID, "tenantId": "other", "productId": PRODUCT_ID, "channelId": CHANNEL_ID},
        {"id": LISTING_ID, "tenantId": TENANT, "productId": "other", "channelId": CHANNEL_ID},
    ]:
        api = make_api(listing)
        framework = WriteToolFramework(secret="x" * 32)
        with patch("tools.products.get_api", return_value=api), pytest.raises(WriteError):
            await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1}, WriteContext("user-1", TENANT))
        api.async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_listing_selection_change_invalidates_commit():
    api = make_api()
    responses = iter([
        {"data": {"id": PRODUCT_ID, "tenantId": TENANT}}, {"data": {"id": LISTING_ID, "tenantId": TENANT, "productId": PRODUCT_ID, "channelId": CHANNEL_ID}},
        {"data": {"id": PRODUCT_ID, "tenantId": TENANT}}, {"data": {"id": "other-listing", "tenantId": TENANT, "productId": PRODUCT_ID, "channelId": CHANNEL_ID}},
    ])
    api.async_get = AsyncMock(side_effect=lambda *a, **k: next(responses))
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1, "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == "STALE_PREVIEW"
    api.async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_listing_confirmation_idempotency_and_backend_error():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1}, context)
        commit = {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1, "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "listing-key"}
        first = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, commit, context)
        second = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, commit, context)
        assert second == {**first, "replayed": True}
        with pytest.raises(WriteError) as replay:
            await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1, "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert replay.value.code == "TOKEN_USED"

    api = make_api()
    api.async_request = AsyncMock(side_effect=httpx.HTTPStatusError("backend", request=httpx.Request("PUT", "https://example.test"), response=httpx.Response(409)))
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_PRODUCT_CHANNEL_LISTING_SPEC, {"product_id": PRODUCT_ID, "channel_id": CHANNEL_ID, "allocation": 1, "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == "CONFLICT"
