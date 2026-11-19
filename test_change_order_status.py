from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.orders import CHANGE_ORDER_STATUS_SPEC

ORDER_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def api_with_transition():
    api = AsyncMock()
    async def async_get(endpoint, **kwargs):
        if endpoint.endswith("status-transitions"):
            return {"data": {"PENDING": ["CONFIRMED", "CANCELLED"]}}
        return {"data": {"id": ORDER_ID, "orderNo": "ORD-1", "orderStatus": "PENDING", "version": 3}}
    api.async_get = AsyncMock(side_effect=async_get)
    api.async_request = AsyncMock(return_value={"id": ORDER_ID, "orderStatus": "CONFIRMED", "version": 4})
    return api


@pytest.mark.asyncio
async def test_change_order_status_preview_does_not_put_and_commit_preserves_response():
    api = api_with_transition()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(
            CHANGE_ORDER_STATUS_SPEC,
            {"order_id": ORDER_ID, "order_status": "CONFIRMED", "notes": "Approved", "version": 3},
            context,
        )
        assert preview["status"] == "preview"
        assert api.async_request.await_count == 0
        committed = await framework.invoke(
            CHANGE_ORDER_STATUS_SPEC,
            {"order_id": ORDER_ID, "order_status": "CONFIRMED", "notes": "Approved", "version": 3, "confirm": True, "confirmation_token": preview["confirmation_token"]},
            context,
        )
    assert committed["result"] == {"id": ORDER_ID, "orderStatus": "CONFIRMED", "version": 4}
    api.async_request.assert_awaited_once_with(
        "PUT", f"/commerce/orders/{ORDER_ID}",
        json={"orderStatus": "CONFIRMED", "notes": "Approved", "version": 3},
        retry=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"order_id": "bad", "order_status": "CONFIRMED"},
        {"order_id": ORDER_ID, "order_status": "NOT_A_STATUS"},
        {"order_id": ORDER_ID, "order_status": "CONFIRMED", "version": "3"},
        {"order_id": ORDER_ID, "order_status": "CONFIRMED", "cancel_reason": 3},
    ],
)
async def test_change_order_status_rejects_invalid_input(args):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(CHANGE_ORDER_STATUS_SPEC, args, WriteContext("user-1", "tenant-1"))


@pytest.mark.asyncio
async def test_change_order_status_rejects_backend_transition_not_allowed():
    api = api_with_transition()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api), pytest.raises(WriteError) as raised:
        await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "DELIVERED"}, WriteContext("user-1", "tenant-1"))
    assert raised.value.code == "TRANSITION_NOT_PERMITTED"
    api.async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_order_status_confirmation_token_and_idempotency():
    api = api_with_transition()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "CONFIRMED"}, context)
        args = {"order_id": ORDER_ID, "order_status": "CONFIRMED", "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "status-key"}
        first = await framework.invoke(CHANGE_ORDER_STATUS_SPEC, args, context)
        second = await framework.invoke(CHANGE_ORDER_STATUS_SPEC, args, context)
        assert second == {**first, "replayed": True}
        assert api.async_request.await_count == 1
        with pytest.raises(WriteError) as replay:
            await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "CONFIRMED", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert replay.value.code == "TOKEN_USED"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, "VALIDATION_FAILED"), (409, "CONFLICT")])
async def test_change_order_status_maps_backend_errors(status, code):
    api = api_with_transition()
    api.async_request = AsyncMock(side_effect=httpx.HTTPStatusError("backend", request=httpx.Request("PUT", "https://example.test"), response=httpx.Response(status)))
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "CONFIRMED"}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "CONFIRMED", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_change_order_status_maps_timeout():
    api = api_with_transition()
    api.async_request = AsyncMock(side_effect=TimeoutError())
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "CONFIRMED"}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(CHANGE_ORDER_STATUS_SPEC, {"order_id": ORDER_ID, "order_status": "CONFIRMED", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == "OUTCOME_UNKNOWN"
