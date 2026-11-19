from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import write_tools
from services.write_tools import WriteContext, WriteError, WriteToolFramework
from tools.orders import ADD_ORDER_NOTE_SPEC, UPDATE_ORDER_TAGS_SPEC

ORDER_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


def _api(response=None, error=None):
    api = AsyncMock()
    api.async_get = AsyncMock(return_value=response or {"data": {"id": ORDER_ID, "orderNo": "ORD-1", "version": 1}})
    api.async_post = AsyncMock(return_value=response or {"data": {"id": "note-1"}})
    if error:
        api.async_post.side_effect = error
    return api


@pytest.mark.asyncio
async def test_add_order_note_preview_and_commit():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Customer called"}, WriteContext("user-1", "tenant-1"))
        result = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Customer called", "confirm": True, "confirmation_token": preview["confirmation_token"]}, WriteContext("user-1", "tenant-1"))
    assert result["result"] == {"success": True, "orderId": ORDER_ID, "message": "Order note created successfully.", "noteId": "note-1"}
    api.async_post.assert_awaited_once_with("/commerce/order-note", {"orderId": ORDER_ID, "note": "Customer called"}, retry=False)


@pytest.mark.asyncio
async def test_add_order_note_preview_never_posts():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(
            ADD_ORDER_NOTE_SPEC,
            {"order_id": ORDER_ID, "note": "Preview only"},
            WriteContext("user-1", "tenant-1"),
        )
    assert preview["status"] == "preview"
    api.async_post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"order_id": ORDER_ID, "note": ""},
        {"order_id": "", "note": "Note"},
        {"order_id": "not-a-uuid", "note": "Note"},
        {"order_id": ORDER_ID, "note": "x" * 2001},
    ],
)
async def test_add_order_note_rejects_invalid_input(args):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError, match="valid UUID|non-empty|must not be empty|at most 2000"):
        await framework.invoke(ADD_ORDER_NOTE_SPEC, args, WriteContext("user-1", "tenant-1"))


@pytest.mark.asyncio
async def test_add_order_note_rejects_wrong_tenant_token():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note"}, WriteContext("user-1", "tenant-1"))
        with pytest.raises(WriteError, match="does not belong"):
            await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note", "confirm": True, "confirmation_token": preview["confirmation_token"]}, WriteContext("user-1", "tenant-2"))


@pytest.mark.asyncio
async def test_add_order_note_requires_confirmation_and_rejects_invalid_token():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note"}, context)
        with pytest.raises(WriteError) as missing:
            await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note", "confirm": True}, context)
        with pytest.raises(WriteError) as invalid:
            await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note", "confirm": True, "confirmation_token": preview["confirmation_token"] + "x"}, context)
    assert missing.value.code == "CONFIRMATION_REQUIRED"
    assert invalid.value.code == "TOKEN_INVALID"
    api.async_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_order_note_maps_backend_error_and_timeout():
    error = httpx.HTTPStatusError("backend", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(500))
    api = _api(error=error)
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note"}, WriteContext("user-1", "tenant-1"))
        with pytest.raises(WriteError) as raised:
            await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note", "confirm": True, "confirmation_token": preview["confirmation_token"]}, WriteContext("user-1", "tenant-1"))
    assert raised.value.code == "BACKEND_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
        [(400, "VALIDATION_FAILED"), (401, "SIGNED_OUT"), (404, "NOT_FOUND")],
)
async def test_add_order_note_maps_client_backend_errors(status, code):
    error = httpx.HTTPStatusError(
        "backend",
        request=httpx.Request("POST", "https://example.test"),
        response=httpx.Response(status),
    )
    api = _api(error=error)
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note"}, context)
        with pytest.raises(WriteError) as raised:
            await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_add_order_note_timeout_is_outcome_unknown():
    api = _api(error=TimeoutError())
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note"}, WriteContext("user-1", "tenant-1"))
        with pytest.raises(WriteError) as raised:
            await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note", "confirm": True, "confirmation_token": preview["confirmation_token"]}, WriteContext("user-1", "tenant-1"))
    assert raised.value.code == "OUTCOME_UNKNOWN"


@pytest.mark.asyncio
async def test_add_order_note_idempotency_replays_without_second_post():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(ADD_ORDER_NOTE_SPEC, {"order_id": ORDER_ID, "note": "Note"}, context)
        args = {
            "order_id": ORDER_ID,
            "note": "Note",
            "confirm": True,
            "confirmation_token": preview["confirmation_token"],
            "idempotency_key": "note-key",
        }
        first = await framework.invoke(ADD_ORDER_NOTE_SPEC, args, context)
        replay = await framework.invoke(ADD_ORDER_NOTE_SPEC, args, context)
    assert replay == {**first, "replayed": True}
    api.async_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_order_tags_preview_and_commit_preserve_backend_response():
    api = _api()
    api.async_request = AsyncMock(return_value=[{"name": "Priority"}])
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(
            UPDATE_ORDER_TAGS_SPEC,
            {"order_id": ORDER_ID, "tags": ["Priority", "Customer Follow-up"]},
            context,
        )
        assert preview["status"] == "preview"
        assert "ALL existing tags" in preview["warnings"][0]
        assert api.async_request.await_count == 0
        committed = await framework.invoke(
            UPDATE_ORDER_TAGS_SPEC,
            {
                "order_id": ORDER_ID,
                "tags": ["Priority", "Customer Follow-up"],
                "confirm": True,
                "confirmation_token": preview["confirmation_token"],
            },
            context,
        )
    assert committed["result"] == [{"name": "Priority"}]
    api.async_request.assert_awaited_once_with(
        "PUT",
        f"/commerce/orders/{ORDER_ID}/tags",
        json={"tags": ["Priority", "Customer Follow-up"]},
        retry=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"order_id": "bad", "tags": []},
        {"order_id": ORDER_ID, "tags": "Priority"},
        {"order_id": ORDER_ID, "tags": ["Priority", 3]},
    ],
)
async def test_update_order_tags_rejects_invalid_input(args):
    framework = WriteToolFramework(secret="x" * 32)
    with pytest.raises(WriteError):
        await framework.invoke(UPDATE_ORDER_TAGS_SPEC, args, WriteContext("user-1", "tenant-1"))


@pytest.mark.asyncio
async def test_update_order_tags_allows_empty_array():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(
            UPDATE_ORDER_TAGS_SPEC,
            {"order_id": ORDER_ID, "tags": []},
            WriteContext("user-1", "tenant-1"),
        )
    assert preview["status"] == "preview"


@pytest.mark.asyncio
async def test_update_order_tags_confirmation_and_single_use():
    api = _api()
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "tags": ["A"]}, context)
        with pytest.raises(WriteError) as missing:
            await framework.invoke(UPDATE_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "tags": ["A"], "confirm": True}, context)
        with pytest.raises(WriteError) as invalid:
            await framework.invoke(UPDATE_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "tags": ["A"], "confirm": True, "confirmation_token": "bad"}, context)
        args = {"order_id": ORDER_ID, "tags": ["A"], "confirm": True, "confirmation_token": preview["confirmation_token"]}
        await framework.invoke(UPDATE_ORDER_TAGS_SPEC, args, context)
        with pytest.raises(WriteError) as replay:
            await framework.invoke(UPDATE_ORDER_TAGS_SPEC, args, context)
    assert missing.value.code == "CONFIRMATION_REQUIRED"
    assert invalid.value.code == "TOKEN_INVALID"
    assert replay.value.code == "TOKEN_USED"


@pytest.mark.asyncio
async def test_update_order_tags_backend_errors_and_timeout():
    for error, expected in [
        (httpx.HTTPStatusError("backend", request=httpx.Request("PUT", "https://example.test"), response=httpx.Response(404)), "NOT_FOUND"),
        (TimeoutError(), "OUTCOME_UNKNOWN"),
    ]:
        api = _api()
        api.async_request = AsyncMock(side_effect=error)
        framework = WriteToolFramework(secret="x" * 32)
        context = WriteContext("user-1", "tenant-1")
        with patch("tools.orders.get_api", return_value=api):
            preview = await framework.invoke(UPDATE_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "tags": ["A"]}, context)
            with pytest.raises(WriteError) as raised:
                await framework.invoke(UPDATE_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "tags": ["A"], "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
        assert raised.value.code == expected


@pytest.mark.asyncio
async def test_update_order_tags_idempotency_replays_without_second_put():
    api = _api()
    api.async_request = AsyncMock(return_value=[])
    framework = WriteToolFramework(secret="x" * 32)
    context = WriteContext("user-1", "tenant-1")
    with patch("tools.orders.get_api", return_value=api):
        preview = await framework.invoke(UPDATE_ORDER_TAGS_SPEC, {"order_id": ORDER_ID, "tags": ["A"]}, context)
        args = {"order_id": ORDER_ID, "tags": ["A"], "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "tag-key"}
        first = await framework.invoke(UPDATE_ORDER_TAGS_SPEC, args, context)
        second = await framework.invoke(UPDATE_ORDER_TAGS_SPEC, args, context)
    assert second == {**first, "replayed": True}
    api.async_request.assert_awaited_once()
