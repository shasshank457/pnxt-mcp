"""Unit and integration tests for the 4 order-management MCP tools:
1. get_order_details
2. find_stuck_orders
3. get_order_status_transitions
4. confirm_order_status_change
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from services.auth_session import AuthSession, invalidate_session, set_session
from services.write_tools import WriteError
from tools.orders import (
    confirm_order_status_change,
    find_stuck_orders,
    get_order_details,
    get_order_status_transitions,
    parse_permitted_transitions,
)

SAMPLE_RAW_ORDER = {
    "id": "order-1082",
    "orderNo": "1000001082",
    "channelOrderNo": "CH-1082",
    "orderDate": "2026-01-10T10:00:00Z",
    "orderStatus": "PENDING",
    "tenantId": "tenant-secret-123",
    "version": 1,
    "customer": {
        "id": "cust-1",
        "firstName": "Arun",
        "lastName": "Sharma",
        "email": "arun.sharma@example.com",
        "phone": "+919876543210",
        "tenantId": "tenant-secret-123",
    },
    "orderItems": [
        {
            "id": "item-1",
            "name": "Wireless Headphones",
            "sku": "WH-100",
            "quantityOrdered": 2,
            "unitPrice": 1500.0,
            "lineTotal": 3000.0,
        }
    ],
    "grandTotal": 3000.0,
    "currency": "INR",
    "paymentMethod": "PREPAID",
    "warehouse": {"id": "wh-1", "code": "BLR-01", "name": "Bangalore Central"},
    "config": {"internal_notes": "Internal configuration data"},
}

SAMPLE_TRANSITIONS = {
    "data": [
        {"from": "PENDING", "to": ["CONFIRMED", "CANCELLED", "ON_HOLD"]},
        {"from": "CONFIRMED", "to": ["PARTIALLY_FULFILLED", "FULFILLED", "CANCELLED"]},
        {"from": "ON_HOLD", "to": ["PENDING", "CANCELLED"]},
        {"from": "FULFILLED", "to": ["DELIVERED", "RETURN_REQUESTED"]},
        {"from": "DELIVERED", "to": ["RETURN_REQUESTED", "CLOSED"]},
    ]
}


@pytest.fixture
def auth_session():
    """Setup authenticated session for testing."""
    session = AuthSession(
        access_token="test-jwt-token",
        refresh_token="test-refresh-token",
        tenant_id="tenant-test-1",
        email="operator@pointnxt.com",
        user_id="user-operator-1",
    )
    with patch("tools.orders.get_session", return_value=session):
        yield session


def build_mock_api(orders=None, transitions=None):
    orders_list = orders if orders is not None else [dict(SAMPLE_RAW_ORDER)]
    transitions_data = transitions if transitions is not None else SAMPLE_TRANSITIONS

    api = MagicMock()

    async def async_get(endpoint: str, params: dict | None = None) -> dict:
        if endpoint == "/commerce/orders":
            status = (params or {}).get("orderStatus")
            search = (params or {}).get("search")
            filtered = list(orders_list)
            if status:
                filtered = [o for o in filtered if o.get("orderStatus") == status]
            if search:
                filtered = [
                    o for o in filtered
                    if o.get("orderNo") == search or o.get("id") == search or o.get("channelOrderNo") == search
                ]
            return {"data": {"items": filtered, "metadata": {"total": len(filtered)}}}
        if endpoint == "/commerce/orders/status-transitions":
            return transitions_data
        if endpoint == "/commerce/orders/stats":
            return {"data": {"total": len(orders_list)}}
        if endpoint.startswith("/commerce/orders/"):
            oid = endpoint.rsplit("/", 1)[-1]
            match = next((o for o in orders_list if o["id"] == oid), None)
            if match:
                return {"data": match}
            import httpx
            req = httpx.Request("GET", f"https://api.test{endpoint}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("Order not found", request=req, response=resp)
        return {"data": {}}

    async def async_request(method: str, endpoint: str, **kwargs) -> dict:
        if method == "PUT" and "/commerce/orders/" in endpoint:
            oid = endpoint.replace("/status", "").rsplit("/", 1)[-1]
            match = next((o for o in orders_list if o["id"] == oid), None)
            if match:
                body = kwargs.get("json", {})
                new_status = body.get("orderStatus") or body.get("status")
                if new_status:
                    match["orderStatus"] = new_status
                match["version"] = match.get("version", 1) + 1
                return {"data": match, "status": "success"}
            import httpx
            req = httpx.Request("PUT", f"https://api.test{endpoint}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("Order not found", request=req, response=resp)
        return {"data": {}}

    api.async_get = AsyncMock(side_effect=async_get)
    api.async_request = AsyncMock(side_effect=async_request)
    return api


# ==============================================================================
# 1. get_order_details Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_order_details_by_id_returns_complete_sanitized_order():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_order_details(order_id="order-1082")

    assert result["found"] is True
    assert result["order_id"] == "order-1082"
    assert result["order_number"] == "1000001082"
    order = result["order"]
    assert order["grandTotal"] == 3000.0
    assert len(order["orderItems"]) == 1
    assert order["orderItems"][0]["sku"] == "WH-100"
    # Ensure PII is masked
    assert order["customer"]["email"] == "a***@example.com"
    assert order["customer"]["phone"] == "***10"
    # Ensure internal config and tenantId are removed
    assert "tenantId" not in order
    assert "config" not in order


@pytest.mark.asyncio
async def test_get_order_details_by_reference_resolves_and_returns():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_order_details(order_reference="#1000001082")

    assert result["found"] is True
    assert result["order_id"] == "order-1082"
    assert result["order"]["orderStatus"] == "PENDING"


@pytest.mark.asyncio
async def test_get_order_details_not_found():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_order_details(order_id="non-existent-order")

    assert result["found"] is False
    assert result["order"] is None
    assert "not found" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_order_details_requires_an_identifier():
    with pytest.raises(ValueError, match="Either order_id or order_reference must be provided"):
        await get_order_details()


# ==============================================================================
# 2. find_stuck_orders Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_find_stuck_orders_identifies_overdue_and_on_track():
    now = datetime.now(timezone.utc)
    stuck_time = (now - timedelta(hours=36)).isoformat().replace("+00:00", "Z")
    recent_time = (now - timedelta(hours=4)).isoformat().replace("+00:00", "Z")

    order_stuck = dict(SAMPLE_RAW_ORDER, id="order-stuck", orderNo="STUCK-1", createdAt=stuck_time, updatedAt=stuck_time)
    order_on_track = dict(SAMPLE_RAW_ORDER, id="order-track", orderNo="TRACK-1", createdAt=recent_time, updatedAt=recent_time)

    api = build_mock_api(orders=[order_stuck, order_on_track])
    with patch("tools.orders.get_api", return_value=api):
        result = await find_stuck_orders(order_status="PENDING", threshold_hours=24)

    assert result["found"] is True
    assert result["stuck_count"] == 1
    assert result["on_track_count"] == 1
    assert result["total_evaluated"] == 2

    stuck_item = result["stuck_orders"][0]
    assert stuck_item["order_number"] == "STUCK-1"
    assert stuck_item["is_overdue"] is True
    assert stuck_item["category"] == "OVERDUE"
    assert stuck_item["age_hours"] >= 35.0

    track_item = result["on_track_orders"][0]
    assert track_item["order_number"] == "TRACK-1"
    assert track_item["is_overdue"] is False
    assert track_item["category"] == "PENDING_ON_TRACK"
    assert track_item["age_hours"] < 10.0


@pytest.mark.asyncio
async def test_find_stuck_orders_configurable_threshold():
    now = datetime.now(timezone.utc)
    time_10h_ago = (now - timedelta(hours=10)).isoformat().replace("+00:00", "Z")
    order = dict(SAMPLE_RAW_ORDER, id="order-10h", orderNo="ORD-10H", createdAt=time_10h_ago, updatedAt=time_10h_ago)

    api = build_mock_api(orders=[order])
    with patch("tools.orders.get_api", return_value=api):
        # With 24h threshold -> not stuck
        res24 = await find_stuck_orders(order_status="PENDING", threshold_hours=24)
        assert res24["found"] is False
        assert res24["stuck_count"] == 0
        assert res24["on_track_count"] == 1

        # With 8h threshold -> stuck!
        res8 = await find_stuck_orders(order_status="PENDING", threshold_hours=8)
        assert res8["found"] is True
        assert res8["stuck_count"] == 1
        assert res8["on_track_count"] == 0


@pytest.mark.asyncio
async def test_find_stuck_orders_validation():
    with pytest.raises(ValueError, match="threshold_hours must be a positive number"):
        await find_stuck_orders(threshold_hours=-5)

    with pytest.raises(ValueError, match="threshold_hours must be a positive number"):
        await find_stuck_orders(threshold_hours=0)

    with pytest.raises(ValueError, match="Invalid order_status"):
        await find_stuck_orders(order_status="NON_EXISTENT_STATUS")


# ==============================================================================
# 3. get_order_status_transitions Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_order_status_transitions_all_and_filtered():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        # Fetch all
        all_res = await get_order_status_transitions()
        assert "permitted_transitions_by_status" in all_res
        assert "PENDING" in all_res["permitted_transitions_by_status"]
        assert "CONFIRMED" in all_res["permitted_transitions_by_status"]["PENDING"]

        # Fetch filtered by current_status
        filtered_res = await get_order_status_transitions(current_status="PENDING")
        assert filtered_res["current_status"] == "PENDING"
        assert set(filtered_res["permitted_next_statuses"]) == {"CANCELLED", "CONFIRMED", "ON_HOLD"}


@pytest.mark.asyncio
async def test_get_order_status_transitions_invalid_status():
    with pytest.raises(ValueError, match="Invalid current_status"):
        await get_order_status_transitions(current_status="UNKNOWN_STATUS")


def test_parse_permitted_transitions_supports_dict_and_list():
    # List format
    res_list = parse_permitted_transitions({
        "data": [{"from": "PENDING", "to": ["CONFIRMED", "CANCELLED"]}]
    })
    assert res_list["PENDING"] == {"CONFIRMED", "CANCELLED"}

    # Dict format
    res_dict = parse_permitted_transitions({
        "data": {"PENDING": ["CONFIRMED", "CANCELLED"], "CONFIRMED": ["FULFILLED"]}
    })
    assert res_dict["PENDING"] == {"CONFIRMED", "CANCELLED"}
    assert res_dict["CONFIRMED"] == {"FULFILLED"}


# ==============================================================================
# 4. confirm_order_status_change Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_confirm_order_status_change_preview_and_commit(auth_session):
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        # 1. Preview
        preview = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            reason="Customer payment verified",
            confirm=False,
        )
        assert preview["status"] == "preview"
        assert preview["order_id"] == "order-1082"
        assert preview["current_status"] == "PENDING"
        assert preview["requested_status"] == "CONFIRMED"
        assert preview["valid_transition"] is True
        token = preview["confirmation_token"]
        assert token and "." in token

        # Verify no write executed yet
        api.async_request.assert_not_awaited()

        # 2. Commit
        committed = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            reason="Customer payment verified",
            confirm=True,
            confirmation_token=token,
        )
        assert committed["status"] == "committed"
        assert committed["previous_status"] == "PENDING"
        assert committed["new_status"] == "CONFIRMED"
        assert committed["audit_id"] is not None

        # Verify write occurred
        api.async_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_order_status_change_rejects_invalid_transition(auth_session):
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        # Order is PENDING. Transition to DELIVERED is not permitted.
        with pytest.raises(WriteError) as exc_info:
            await confirm_order_status_change(
                order_id="order-1082",
                new_status="DELIVERED",
                confirm=False,
            )
        assert exc_info.value.code == "TRANSITION_NOT_PERMITTED"
        assert "not permitted" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_confirm_order_status_change_order_not_found(auth_session):
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        with pytest.raises(WriteError) as exc_info:
            await confirm_order_status_change(
                order_id="missing-order",
                new_status="CONFIRMED",
                confirm=False,
            )
        assert exc_info.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_confirm_order_status_change_auth_required():
    with patch("tools.orders.get_session", return_value=None), \
         patch("config.DEV_MODE", False):
        with pytest.raises(WriteError) as exc_info:
            await confirm_order_status_change(
                order_id="order-1082",
                new_status="CONFIRMED",
                confirm=False,
            )
        assert exc_info.value.code == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_confirm_order_status_change_token_required_for_commit(auth_session):
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        with pytest.raises(WriteError) as exc_info:
            await confirm_order_status_change(
                order_id="order-1082",
                new_status="CONFIRMED",
                confirm=True,
            )
        assert exc_info.value.code == "CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_confirm_order_status_change_concurrency_record_changed(auth_session):
    order_data = dict(SAMPLE_RAW_ORDER, version=1)
    api = build_mock_api(orders=[order_data])
    with patch("tools.orders.get_api", return_value=api):
        preview = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=False,
        )
        token = preview["confirmation_token"]

        # Simulate concurrent change to version 2
        order_data["version"] = 2

        with pytest.raises(WriteError) as exc_info:
            await confirm_order_status_change(
                order_id="order-1082",
                new_status="CONFIRMED",
                confirm=True,
                confirmation_token=token,
            )
        assert exc_info.value.code == "RECORD_CHANGED"


@pytest.mark.asyncio
async def test_confirm_order_status_change_token_reuse_rejected(auth_session):
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        preview = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=False,
        )
        token = preview["confirmation_token"]

        # First commit succeeds
        await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=True,
            confirmation_token=token,
        )

        # Second commit with same token must fail with TOKEN_USED
        with pytest.raises(WriteError) as exc_info:
            await confirm_order_status_change(
                order_id="order-1082",
                new_status="CONFIRMED",
                confirm=True,
                confirmation_token=token,
            )
        assert exc_info.value.code == "TOKEN_USED"


@pytest.mark.asyncio
async def test_confirm_order_status_change_idempotency_key(auth_session):
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        preview = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=False,
        )
        token = preview["confirmation_token"]

        # Commit with idempotency_key
        res1 = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=True,
            confirmation_token=token,
            idempotency_key="idemp-key-abc",
        )
        assert res1["status"] == "committed"

        # Retry commit with same idempotency_key returns cached result
        res2 = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=True,
            confirmation_token=token,
            idempotency_key="idemp-key-abc",
        )
        assert res2 == res1


@pytest.mark.asyncio
async def test_confirm_order_status_change_idempotent_no_op_when_already_in_status(auth_session):
    order_data = dict(SAMPLE_RAW_ORDER, orderStatus="CONFIRMED")
    api = build_mock_api(orders=[order_data])
    with patch("tools.orders.get_api", return_value=api):
        # Preview warns it is already in that status
        preview = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=False,
        )
        assert preview["status"] == "preview"
        assert "already in status" in preview["warning"]

        # Commit is a safe idempotent no-op without backend error
        committed = await confirm_order_status_change(
            order_id="order-1082",
            new_status="CONFIRMED",
            confirm=True,
            confirmation_token=preview["confirmation_token"],
        )
        assert committed["status"] == "committed"
        assert committed["idempotent"] is True
