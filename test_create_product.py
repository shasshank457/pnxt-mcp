from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.products import CREATE_PRODUCT_SPEC

TENANT = "tenant-123"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def make_api():
    api = AsyncMock()
    api.async_post = AsyncMock(return_value={"data": {"id": "product-1", "sku": "SKU-1"}})
    return api


def args():
    return {"code": "P-1", "sku": "SKU-1", "name": "Widget", "price": 10.5, "weight": 2, "weight_unit_code": "KG", "category_id": "11111111-1111-4111-8111-111111111111"}


@pytest.mark.asyncio
async def test_create_product_preview_hides_tenant_and_does_not_post():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(CREATE_PRODUCT_SPEC, args(), WriteContext("user-1", TENANT))
    assert preview["status"] == "preview"
    assert "tenantId" not in preview["proposed_changes"]
    api.async_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_product_commit_includes_context_tenant_and_preserves_response():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(CREATE_PRODUCT_SPEC, args(), context)
        committed = await framework.invoke(CREATE_PRODUCT_SPEC, {**args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert committed["result"] == {"data": {"id": "product-1", "sku": "SKU-1"}}
    payload = dict(args())
    payload["weightUnitCode"] = payload.pop("weight_unit_code")
    payload["categoryId"] = payload.pop("category_id")
    payload["tenantId"] = TENANT
    api.async_post.assert_awaited_once_with("/commerce/products", payload, retry=False)


@pytest.mark.asyncio
async def test_create_product_schema_has_no_tenant_input():
    assert "tenantId" not in CREATE_PRODUCT_SPEC.input_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"code": "", "sku": "SKU", "name": "Name"},
    {"code": "C", "sku": "", "name": "Name"},
    {"code": "C", "sku": "SKU", "name": "Name", "weight": 0},
    {"code": "C", "sku": "SKU", "name": "Name", "price": float("inf")},
    {"code": "C", "sku": "SKU", "name": "Name", "weight_unit_code": "FT", "weight": 1},
    {"code": "C", "sku": "SKU", "name": "Name", "category_id": "not-uuid"},
    {"code": "C", "sku": "SKU", "name": "Name", "is_fragile": "yes"},
])
async def test_create_product_validation(bad):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(CREATE_PRODUCT_SPEC, bad, WriteContext("user-1", TENANT))


@pytest.mark.asyncio
async def test_create_product_requires_tenant_context():
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError, match="tenant"):
        await framework.invoke(CREATE_PRODUCT_SPEC, args(), WriteContext("user-1", ""))


@pytest.mark.asyncio
async def test_create_product_token_reuse_and_idempotency():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(CREATE_PRODUCT_SPEC, args(), context)
        commit = {**args(), "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "product-key"}
        first = await framework.invoke(CREATE_PRODUCT_SPEC, commit, context)
        second = await framework.invoke(CREATE_PRODUCT_SPEC, commit, context)
        assert second == {**first, "replayed": True}
        with pytest.raises(WriteError) as replay:
            await framework.invoke(CREATE_PRODUCT_SPEC, {**args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert replay.value.code == "TOKEN_USED"
    api.async_post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, "VALIDATION_FAILED"), (404, "NOT_FOUND"), (401, "SIGNED_OUT")])
async def test_create_product_backend_errors(status, code):
    api = make_api()
    api.async_post = AsyncMock(side_effect=httpx.HTTPStatusError("backend", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(status)))
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", TENANT)
    with patch("tools.products.get_api", return_value=api):
        preview = await framework.invoke(CREATE_PRODUCT_SPEC, args(), context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(CREATE_PRODUCT_SPEC, {**args(), "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code
