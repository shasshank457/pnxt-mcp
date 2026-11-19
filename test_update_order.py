from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.orders import UPDATE_ORDER_SPEC

ORDER_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def make_api():
    api = AsyncMock()
    api.async_get = AsyncMock(return_value={"data": {"id": ORDER_ID, "orderNo": "ORD-1", "version": 3}})
    api.async_request = AsyncMock(return_value={"data": {"id": ORDER_ID, "version": 4}})
    return api


@pytest.mark.asyncio
async def test_update_order_preview_and_partial_commit_uses_captured_version():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "order_weight": 2.5, "weight_unit_code": "KG"}, context)
        assert preview["status"] == "preview"
        api.async_request.assert_not_awaited()
        committed = await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "order_weight": 2.5, "weight_unit_code": "KG", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert committed["result"] == {"data": {"id": ORDER_ID, "version": 4}}
    api.async_request.assert_awaited_once_with("PUT", f"/commerce/orders/{ORDER_ID}", json={"orderWeight": 2.5, "weightUnitCode": "KG", "version": 3}, retry=False)
    assert api.async_get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"order_id": "bad", "order_weight": 1},
    {"order_id": ORDER_ID, "weight_unit_code": "KG"},
    {"order_id": ORDER_ID, "order_weight": 0},
    {"order_id": ORDER_ID, "order_weight": float("nan")},
    {"order_id": ORDER_ID, "dimension_height": float("inf")},
    {"order_id": ORDER_ID, "measure_unit_code": "FT", "dimension_height": 1},
    {"order_id": ORDER_ID, "order_weight": 1, "weight_unit_code": "FT"},
])
async def test_update_order_validation(args):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(UPDATE_ORDER_SPEC, args, WriteContext("user-1", "tenant-1"))


@pytest.mark.asyncio
async def test_update_order_confirmation_token_reuse_and_idempotency():
    api = make_api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "fulfillment_instruction": "Pack carefully"}, context)
        args = {"order_id": ORDER_ID, "fulfillment_instruction": "Pack carefully", "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "update-key"}
        first = await framework.invoke(UPDATE_ORDER_SPEC, args, context)
        second = await framework.invoke(UPDATE_ORDER_SPEC, args, context)
        assert second == {**first, "replayed": True}
        with pytest.raises(WriteError) as replay:
            await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "fulfillment_instruction": "Pack carefully", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert replay.value.code == "TOKEN_USED"
    api.async_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, "VALIDATION_FAILED"), (404, "NOT_FOUND"), (409, "CONFLICT"), (401, "SIGNED_OUT")])
async def test_update_order_backend_errors(status, code):
    api = make_api()
    api.async_request = AsyncMock(side_effect=httpx.HTTPStatusError("backend", request=httpx.Request("PUT", "https://example.test"), response=httpx.Response(status)))
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "order_weight": 1}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "order_weight": 1, "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_update_order_timeout_is_outcome_unknown():
    api = make_api()
    api.async_request = AsyncMock(side_effect=TimeoutError())
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "dimension_length": 1}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(UPDATE_ORDER_SPEC, {"order_id": ORDER_ID, "dimension_length": 1, "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == "OUTCOME_UNKNOWN"
