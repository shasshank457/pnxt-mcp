from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.products import UPDATE_PRODUCT_SPEC

PRODUCT_ID = "11111111-1111-4111-8111-111111111111"
CATEGORY_ID = "22222222-2222-4222-8222-222222222222"
TENANT = "tenant-1"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def valid_args():
    return {"product_id": PRODUCT_ID, "name": "Updated", "subtitle": "New", "description": "Desc", "image": "img", "hscode": "HS", "coo": "IN", "tax_code": "T1", "classification_code": "C1", "weight": 2, "weight_unit_code": "KG", "dimension_length": 1, "dimension_breadth": 2, "dimension_height": 3, "measure_unit_code": "CM", "is_fragile": True, "category_id": CATEGORY_ID}


def make_api(tenant=TENANT):
    api = AsyncMock()
    api.async_get = AsyncMock(return_value={"data": {"id": PRODUCT_ID, "sku": "SKU-1", "tenantId": tenant}})
    api.async_request = AsyncMock(return_value={"data": {"id": PRODUCT_ID, "name": "Updated"}})
    return api


@pytest.mark.asyncio
async def test_update_product_preview_checks_tenant_and_does_not_put():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_SPEC, valid_args(), WriteContext("user-1", TENANT))
    assert preview["status"] == "preview"
    assert preview["proposed_changes"]["name"] == "Updated"
    assert "tenantId" not in preview["proposed_changes"]
    api.async_request.assert_not_awaited()
    api.async_get.assert_awaited_once_with(f"/commerce/products/{PRODUCT_ID}")


@pytest.mark.asyncio
async def test_update_product_commit_rechecks_tenant_and_sends_only_approved_fields():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "Updated"}, context)
        result = await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "Updated", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert result["result"] == {"data": {"id": PRODUCT_ID, "name": "Updated"}}
    api.async_request.assert_awaited_once_with("PUT", f"/commerce/products/{PRODUCT_ID}", json={"name": "Updated"}, retry=False)
    assert api.async_get.await_count == 2


def test_update_product_schema_excludes_tenant_and_forbidden_fields():
    fields = set(UPDATE_PRODUCT_SPEC.input_schema["properties"])
    assert not fields.intersection({"tenantId", "channelIds", "parentId", "manageStock", "code", "sku", "barcode", "status", "price", "mrp", "costPrice", "taxPercent", "vendorId", "config"})


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"product_id": "bad", "name": "x"}, {"product_id": PRODUCT_ID}, {"product_id": PRODUCT_ID, "weight_unit_code": "KG"},
    {"product_id": PRODUCT_ID, "name": None}, {"product_id": PRODUCT_ID, "weight": 0}, {"product_id": PRODUCT_ID, "weight": -1},
    {"product_id": PRODUCT_ID, "weight": float("nan")}, {"product_id": PRODUCT_ID, "weight": float("inf")},
    {"product_id": PRODUCT_ID, "measure_unit_code": "FT", "dimension_length": 1}, {"product_id": PRODUCT_ID, "category_id": "bad"},
    {"product_id": PRODUCT_ID, "is_fragile": "yes", "name": "x"}, {"product_id": PRODUCT_ID, "name": 2},
])
async def test_update_product_rejects_invalid_inputs(bad):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(UPDATE_PRODUCT_SPEC, bad, WriteContext("user-1", TENANT))


@pytest.mark.asyncio
async def test_update_product_requires_tenant_and_product_tenant_verification():
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x"}, WriteContext("user-1", ""))
    for product_tenant, expected in [(None, "AUTH_REQUIRED"), ("other-tenant", "NOT_PERMITTED")]:
        api = make_api(product_tenant)
        with patch("tools.products.get_api", return_value=api), pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x"}, WriteContext("user-1", TENANT))
        assert raised.value.code == expected
        api.async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_product_failed_commit_tenant_recheck_prevents_put():
    api = make_api()
    api.async_get = AsyncMock(side_effect=[
        {"data": {"id": PRODUCT_ID, "tenantId": TENANT}},
        {"data": {"id": PRODUCT_ID, "tenantId": "other-tenant"}},
    ])
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x"}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == "NOT_PERMITTED"
    api.async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_product_confirmation_reuse_and_idempotency():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x"}, context)
        args = {"product_id": PRODUCT_ID, "name": "x", "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "same"}
        first = await framework.invoke(UPDATE_PRODUCT_SPEC, args, context)
        second = await framework.invoke(UPDATE_PRODUCT_SPEC, args, context)
        assert first == second
        with pytest.raises(WriteError) as replay:
            await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert replay.value.code == "TOKEN_USED"
    api.async_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, "VALIDATION_ERROR"), (401, "AUTH_REQUIRED"), (404, "NOT_FOUND"), (409, "CONFLICT")])
async def test_update_product_backend_errors(status, code):
    api = make_api()
    api.async_request = AsyncMock(side_effect=httpx.HTTPStatusError("backend", request=httpx.Request("PUT", "https://example.test"), response=httpx.Response(status)))
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x"}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_PRODUCT_SPEC, {"product_id": PRODUCT_ID, "name": "x", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code
