"""Unit and integration tests for the 4 shipment-management MCP tools:
1. get_shipment_tracking
2. find_delayed_shipments
3. find_untracked_shipments
4. assign_courier
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.auth_session import AuthSession
from services.write_tools import WriteError
from tools.shipments import (
    assign_courier,
    find_delayed_shipments,
    find_untracked_shipments,
    get_shipment_tracking,
)

SAMPLE_RAW_SHIPMENT = {
    "id": "ship-101",
    "shipmentId": "ship-101",
    "awb": "5129110116034",
    "awbNo": "5129110116034",
    "courierId": "DELHIVERY",
    "courierName": "Delhivery Express",
    "shipStatus": "READY_FOR_PICKUP",
    "pieceCount": 1,
    "weightGrams": 500,
    "createdAt": "2026-01-10T10:00:00Z",
    "expectedDeliveryDate": "2026-01-15T18:00:00Z",
    "deliveredAt": None,
    "trackingUrl": "https://track.delhivery.com/5129110116034",
    "trackingEvents": [
        {
            "status": "MANIFESTED",
            "timestamp": "2026-01-10T10:30:00Z",
            "location": "Bangalore Central",
            "activity": "Shipment manifested",
        }
    ],
}

SAMPLE_RAW_ORDER = {
    "id": "order-1082",
    "orderNo": "1000001082",
    "channelOrderNo": "CH-1082",
    "orderDate": "2026-01-10T10:00:00Z",
    "orderStatus": "CONFIRMED",
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
    "shipments": [dict(SAMPLE_RAW_SHIPMENT)],
    "warehouse": {"id": "wh-1", "code": "BLR-01", "name": "Bangalore Central"},
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
    with patch("tools.shipments.get_session", return_value=session):
        yield session


def build_mock_api(orders=None):
    orders_list = orders if orders is not None else [dict(SAMPLE_RAW_ORDER)]
    api = MagicMock()

    async def async_get(endpoint: str, params: dict | None = None) -> dict:
        if endpoint == "/commerce/orders":
            status = (params or {}).get("orderStatus")
            search = (params or {}).get("search") or (params or {}).get("order_no")
            filtered = list(orders_list)
            if status:
                filtered = [o for o in filtered if o.get("orderStatus") == status]
            if search:
                filtered = [
                    o for o in filtered
                    if o.get("orderNo") == search or o.get("id") == search or o.get("channelOrderNo") == search
                ]
            page = (params or {}).get("page", 1)
            limit = (params or {}).get("limit", 20)
            start = (page - 1) * limit
            items = filtered[start : start + limit]
            return {"data": {"items": items, "metadata": {"total": len(filtered), "totalItems": len(filtered)}}}
        if endpoint.startswith("/commerce/orders/"):
            oid = endpoint.rsplit("/", 1)[-1]
            match = next((o for o in orders_list if o["id"] == oid or o.get("orderNo") == oid), None)
            if match:
                return {"data": match}
            req = httpx.Request("GET", f"https://api.test{endpoint}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("Order not found", request=req, response=resp)
        return {"data": {}}

    async def async_request(method: str, endpoint: str, **kwargs) -> dict:
        if method == "PUT" and "/commerce/orders/" in endpoint:
            # Check if testing unsupported endpoint
            if kwargs.get("headers", {}).get("X-Simulate-Unsupported") or "unsupported" in endpoint:
                req = httpx.Request("PUT", f"https://api.test{endpoint}")
                resp = httpx.Response(405, request=req)
                raise httpx.HTTPStatusError("Method Not Allowed", request=req, response=resp)

            oid = endpoint.split("/commerce/orders/")[1].split("/")[0]
            match = next((o for o in orders_list if o["id"] == oid or o.get("orderNo") == oid), None)
            if match:
                body = kwargs.get("json", {})
                courier_id = body.get("courierId")
                courier_name = body.get("courierName")
                for s in (match.get("shipments") or []):
                    if courier_id:
                        s["courierId"] = courier_id
                    if courier_name:
                        s["courierName"] = courier_name
                match["version"] = match.get("version", 1) + 1
                return {"data": match, "status": "success"}

            req = httpx.Request("PUT", f"https://api.test{endpoint}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("Order not found", request=req, response=resp)

        return {"data": {}}

    api.async_get = AsyncMock(side_effect=async_get)
    api.async_request = AsyncMock(side_effect=async_request)
    return api


# ==============================================================================
# 1. get_shipment_tracking Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_shipment_tracking_by_order_reference_success():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_shipment_tracking(order_reference="#1000001082")

    assert result["found"] is True
    assert result["order_id"] == "order-1082"
    assert result["order_number"] == "1000001082"
    assert result["tracking_available"] is True
    assert result["tracking_status"] == "AVAILABLE"
    assert result["tracking_refresh"] == "not_performed"

    tracking = result["tracking"]
    assert tracking["awb"] == "5129110116034"
    assert tracking["courier"] == "Delhivery Express"
    assert tracking["ship_status"] == "READY_FOR_PICKUP"
    assert len(tracking["events"]) == 1
    assert tracking["events"][0]["status"] == "MANIFESTED"

    # Verify PII masking and tenantId removal
    assert "tenantId" not in result
    assert "tenantId" not in result["shipment"]


@pytest.mark.asyncio
async def test_get_shipment_tracking_by_shipment_id():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_shipment_tracking(shipment_id="ship-101")

    assert result["found"] is True
    assert result["shipment_id"] == "ship-101"
    assert result["tracking"]["awb"] == "5129110116034"


@pytest.mark.asyncio
async def test_get_shipment_tracking_by_awb():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_shipment_tracking(awb="5129110116034")

    assert result["found"] is True
    assert result["tracking"]["awb"] == "5129110116034"


@pytest.mark.asyncio
async def test_get_shipment_tracking_order_has_no_shipments():
    order_no_shipments = dict(SAMPLE_RAW_ORDER, id="order-no-ship", orderNo="NO-SHIP-1", shipments=[])
    api = build_mock_api(orders=[order_no_shipments])
    with patch("tools.orders.get_api", return_value=api):
        result = await get_shipment_tracking(order_reference="NO-SHIP-1")

    assert result["found"] is True
    assert result["tracking_available"] is False
    assert result["tracking_status"] == "NO_SHIPMENTS"
    assert result["tracking"] is None


@pytest.mark.asyncio
async def test_get_shipment_tracking_shipment_lacks_tracking_data():
    shipment_no_awb = dict(
        SAMPLE_RAW_SHIPMENT,
        awb=None,
        awbNo=None,
        trackingNumber=None,
        trackingEvents=[],
    )
    order_untracked = dict(SAMPLE_RAW_ORDER, id="order-untrack", orderNo="UNTRACK-1", shipments=[shipment_no_awb])
    api = build_mock_api(orders=[order_untracked])
    with patch("tools.orders.get_api", return_value=api):
        result = await get_shipment_tracking(order_reference="UNTRACK-1")

    assert result["found"] is True
    assert result["tracking_available"] is False
    assert result["tracking_status"] == "UNAVAILABLE"
    assert "not currently available" in result["message"]


@pytest.mark.asyncio
async def test_get_shipment_tracking_order_not_found():
    api = build_mock_api()
    with patch("tools.orders.get_api", return_value=api):
        result = await get_shipment_tracking(order_reference="NON_EXISTENT")

    assert result["found"] is False
    assert result["tracking"] is None
    assert "not found" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_shipment_tracking_requires_at_least_one_identifier():
    with pytest.raises(ValueError, match="At least one of order_reference, shipment_id, or awb must be provided"):
        await get_shipment_tracking()


@pytest.mark.asyncio
async def test_get_shipment_tracking_distinguishes_backend_failure_from_unavailable_tracking():
    api = MagicMock()
    req = httpx.Request("GET", "https://api.test/commerce/orders/500")
    resp = httpx.Response(500, request=req)
    api.async_get = AsyncMock(side_effect=httpx.HTTPStatusError("Internal Server Error", request=req, response=resp))

    with patch("tools.orders.get_api", return_value=api):
        with pytest.raises(httpx.HTTPStatusError):
            await get_shipment_tracking(order_reference="500")


# ==============================================================================
# 2. find_delayed_shipments Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_find_delayed_shipments_by_expected_delivery_date():
    now = datetime.now(timezone.utc)
    past_edd = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    future_edd = (now + timedelta(days=2)).isoformat().replace("+00:00", "Z")

    delayed_ship = dict(SAMPLE_RAW_SHIPMENT, id="ship-delayed", expectedDeliveryDate=past_edd)
    on_track_ship = dict(SAMPLE_RAW_SHIPMENT, id="ship-ontrack", expectedDeliveryDate=future_edd)

    order1 = dict(SAMPLE_RAW_ORDER, id="ord-1", orderNo="ORD-1", shipments=[delayed_ship])
    order2 = dict(SAMPLE_RAW_ORDER, id="ord-2", orderNo="ORD-2", shipments=[on_track_ship])

    api = build_mock_api(orders=[order1, order2])
    with patch("tools.orders.get_api", return_value=api):
        result = await find_delayed_shipments()

    assert result["found"] is True
    assert result["delayed_count"] == 1
    assert result["on_track_count"] == 1
    assert result["total_evaluated"] == 2

    item = result["delayed_shipments"][0]
    assert item["shipment_id"] == "ship-delayed"
    assert item["is_delayed"] is True
    assert item["delay_basis"] == "EXPECTED_DELIVERY_DATE"
    assert item["delay_hours"] >= 47.0
    assert item["fallback_logic_used"] is False


@pytest.mark.asyncio
async def test_find_delayed_shipments_fallback_transit_threshold():
    now = datetime.now(timezone.utc)
    old_date = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    recent_date = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")

    # Missing expectedDeliveryDate on both
    ship_old = dict(SAMPLE_RAW_SHIPMENT, id="ship-old", expectedDeliveryDate=None, createdAt=old_date)
    ship_recent = dict(SAMPLE_RAW_SHIPMENT, id="ship-recent", expectedDeliveryDate=None, createdAt=recent_date)

    order1 = dict(SAMPLE_RAW_ORDER, id="ord-old", orderNo="ORD-OLD", shipments=[ship_old])
    order2 = dict(SAMPLE_RAW_ORDER, id="ord-recent", orderNo="ORD-RECENT", shipments=[ship_recent])

    api = build_mock_api(orders=[order1, order2])
    with patch("tools.orders.get_api", return_value=api):
        # Default 7 days threshold -> ship_old (10 days) is delayed, ship_recent (2 days) is on-track
        result = await find_delayed_shipments(threshold_days=7.0)

    assert result["delayed_count"] == 1
    assert result["on_track_count"] == 1
    delayed = result["delayed_shipments"][0]
    assert delayed["shipment_id"] == "ship-old"
    assert delayed["is_delayed"] is True
    assert delayed["delay_basis"] == "TRANSIT_THRESHOLD_FALLBACK"
    assert delayed["fallback_logic_used"] is True


@pytest.mark.asyncio
async def test_find_delayed_shipments_never_marks_delayed_solely_due_to_missing_events():
    now = datetime.now(timezone.utc)
    future_edd = (now + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    # Future EDD, but 0 tracking events!
    shipment_no_events = dict(
        SAMPLE_RAW_SHIPMENT,
        id="ship-no-events",
        expectedDeliveryDate=future_edd,
        trackingEvents=[],
    )
    order = dict(SAMPLE_RAW_ORDER, id="ord-ne", orderNo="ORD-NE", shipments=[shipment_no_events])

    api = build_mock_api(orders=[order])
    with patch("tools.orders.get_api", return_value=api):
        result = await find_delayed_shipments()

    # Must NOT be classified as delayed
    assert result["delayed_count"] == 0
    assert result["on_track_count"] == 1
    assert result["on_track_shipments"][0]["is_delayed"] is False


@pytest.mark.asyncio
async def test_find_delayed_shipments_excludes_delivered():
    past_edd = "2026-01-01T00:00:00Z"
    delivered_shipment = dict(
        SAMPLE_RAW_SHIPMENT,
        id="ship-delivered",
        shipStatus="DELIVERED",
        deliveredAt="2026-01-02T00:00:00Z",
        expectedDeliveryDate=past_edd,
    )
    order = dict(SAMPLE_RAW_ORDER, id="ord-del", shipments=[delivered_shipment])

    api = build_mock_api(orders=[order])
    with patch("tools.orders.get_api", return_value=api):
        result = await find_delayed_shipments()

    assert result["delayed_count"] == 0
    assert result["total_evaluated"] == 0


@pytest.mark.asyncio
async def test_find_delayed_shipments_invalid_threshold():
    with pytest.raises(ValueError, match="threshold_days must be a positive number"):
        await find_delayed_shipments(threshold_days=-2)

    with pytest.raises(ValueError, match="threshold_hours must be a positive number"):
        await find_delayed_shipments(threshold_hours=0)


# ==============================================================================
# 3. find_untracked_shipments Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_find_untracked_shipments_distinguishes_issues():
    # 1. Missing tracking number (AWB is None)
    ship_missing_awb = dict(
        SAMPLE_RAW_SHIPMENT,
        id="ship-no-awb",
        awb=None,
        awbNo="",
        trackingNumber=None,
        courierName="Delhivery",
        trackingEvents=[],
    )
    # 2. Missing courier assignment (Courier is None)
    ship_missing_courier = dict(
        SAMPLE_RAW_SHIPMENT,
        id="ship-no-courier",
        awb="5129110116034",
        courierId=None,
        courierName=None,
        carrier=None,
        trackingEvents=[],
    )
    # 3. Both missing
    ship_missing_both = dict(
        SAMPLE_RAW_SHIPMENT,
        id="ship-no-both",
        awb=None,
        awbNo=None,
        trackingNumber=None,
        courierId=None,
        courierName=None,
        trackingEvents=[],
    )
    # 4. Courier and AWB exist, but NO tracking events
    ship_no_events = dict(
        SAMPLE_RAW_SHIPMENT,
        id="ship-no-events",
        awb="5129110116034",
        courierName="Delhivery",
        trackingEvents=[],
    )
    # 5. Fully tracked
    ship_fully_tracked = dict(SAMPLE_RAW_SHIPMENT, id="ship-tracked")

    orders = [
        dict(SAMPLE_RAW_ORDER, id="o1", orderNo="O-1", shipments=[ship_missing_awb]),
        dict(SAMPLE_RAW_ORDER, id="o2", orderNo="O-2", shipments=[ship_missing_courier]),
        dict(SAMPLE_RAW_ORDER, id="o3", orderNo="O-3", shipments=[ship_missing_both]),
        dict(SAMPLE_RAW_ORDER, id="o4", orderNo="O-4", shipments=[ship_no_events]),
        dict(SAMPLE_RAW_ORDER, id="o5", orderNo="O-5", shipments=[ship_fully_tracked]),
    ]

    api = build_mock_api(orders=orders)
    with patch("tools.orders.get_api", return_value=api):
        result = await find_untracked_shipments()

    assert result["found"] is True
    assert result["total_untracked"] == 4
    assert result["total_tracked"] == 1
    assert result["total_evaluated"] == 5

    summary = result["summary_by_issue"]
    assert summary["missing_tracking_number_count"] == 2  # ship_missing_awb and ship_missing_both
    assert summary["missing_courier_count"] == 2          # ship_missing_courier and ship_missing_both
    assert summary["absent_tracking_events_count"] == 1   # ship_no_events


@pytest.mark.asyncio
async def test_find_untracked_shipments_invalid_status():
    with pytest.raises(ValueError, match="Invalid ship_status"):
        await find_untracked_shipments(ship_status="INVALID_STATUS")


# ==============================================================================
# 4. assign_courier Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_assign_courier_preview_and_commit(auth_session):
    unassigned_shipment = dict(SAMPLE_RAW_SHIPMENT, courierId=None, courierName=None)
    order_data = dict(SAMPLE_RAW_ORDER, shipments=[unassigned_shipment])
    api = build_mock_api(orders=[order_data])

    with patch("tools.orders.get_api", return_value=api):
        # 1. Preview
        preview = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            courier_name="Blue Dart Express",
            service_type="EXPRESS",
            confirm=False,
        )
        assert preview["status"] == "preview"
        assert preview["action"] == "assign_courier"
        assert preview["order_id"] == "order-1082"
        assert preview["requested_courier_id"] == "BLUEDART"
        token = preview["confirmation_token"]
        assert token and "." in token

        # Verify no write executed during preview
        api.async_request.assert_not_awaited()

        # 2. Commit
        committed = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            courier_name="Blue Dart Express",
            service_type="EXPRESS",
            confirm=True,
            confirmation_token=token,
        )
        assert committed["status"] == "committed"
        assert committed["courier_id"] == "BLUEDART"
        assert committed["courier_name"] == "Blue Dart Express"
        assert committed["audit_id"] is not None

        # Verify backend PUT executed
        api.async_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_assign_courier_duplicate_assignment_idempotent(auth_session):
    assigned_shipment = dict(SAMPLE_RAW_SHIPMENT, courierId="DELHIVERY", courierName="Delhivery Express")
    order_data = dict(SAMPLE_RAW_ORDER, shipments=[assigned_shipment])
    api = build_mock_api(orders=[order_data])

    with patch("tools.orders.get_api", return_value=api):
        # Preview warns it is already assigned
        preview = await assign_courier(
            order_id="order-1082",
            courier_id="DELHIVERY",
            confirm=False,
        )
        assert preview["status"] == "preview"
        assert preview["duplicate_assignment"] is True
        assert "already has courier" in preview["warning"]

        # Commit succeeds as an idempotent no-op without backend error
        committed = await assign_courier(
            order_id="order-1082",
            courier_id="DELHIVERY",
            confirm=True,
            confirmation_token=preview["confirmation_token"],
        )
        assert committed["status"] == "committed"
        assert committed["duplicate_assignment"] is True
        assert committed["idempotent"] is True
        api.async_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_courier_token_reuse_rejected(auth_session):
    unassigned_shipment = dict(SAMPLE_RAW_SHIPMENT, courierId=None, courierName=None)
    order_data = dict(SAMPLE_RAW_ORDER, shipments=[unassigned_shipment])
    api = build_mock_api(orders=[order_data])

    with patch("tools.orders.get_api", return_value=api):
        preview = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=False,
        )
        token = preview["confirmation_token"]

        # First commit succeeds
        await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=True,
            confirmation_token=token,
        )

        # Second commit with same token must fail with TOKEN_USED
        with pytest.raises(WriteError) as exc_info:
            await assign_courier(
                order_id="order-1082",
                courier_id="BLUEDART",
                confirm=True,
                confirmation_token=token,
            )
        assert exc_info.value.code == "TOKEN_USED"


@pytest.mark.asyncio
async def test_assign_courier_idempotency_key(auth_session):
    unassigned_shipment = dict(SAMPLE_RAW_SHIPMENT, courierId=None, courierName=None)
    order_data = dict(SAMPLE_RAW_ORDER, shipments=[unassigned_shipment])
    api = build_mock_api(orders=[order_data])

    with patch("tools.orders.get_api", return_value=api):
        preview = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=False,
        )
        token = preview["confirmation_token"]

        res1 = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=True,
            confirmation_token=token,
            idempotency_key="idemp-courier-123",
        )
        assert res1["status"] == "committed"

        # Retry returns identical cached result
        res2 = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=True,
            confirmation_token=token,
            idempotency_key="idemp-courier-123",
        )
        assert res2 == res1


@pytest.mark.asyncio
async def test_assign_courier_concurrency_record_changed(auth_session):
    order_data = dict(SAMPLE_RAW_ORDER, version=1)
    api = build_mock_api(orders=[order_data])

    with patch("tools.orders.get_api", return_value=api):
        preview = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=False,
        )
        token = preview["confirmation_token"]

        # Simulate concurrent change
        order_data["version"] = 2

        with pytest.raises(WriteError) as exc_info:
            await assign_courier(
                order_id="order-1082",
                courier_id="BLUEDART",
                confirm=True,
                confirmation_token=token,
            )
        assert exc_info.value.code == "RECORD_CHANGED"


@pytest.mark.asyncio
async def test_assign_courier_invalid_shipment_status(auth_session):
    delivered_shipment = dict(SAMPLE_RAW_SHIPMENT, shipStatus="DELIVERED")
    order_data = dict(SAMPLE_RAW_ORDER, shipments=[delivered_shipment])
    api = build_mock_api(orders=[order_data])

    with patch("tools.orders.get_api", return_value=api):
        with pytest.raises(WriteError) as exc_info:
            await assign_courier(
                order_id="order-1082",
                courier_id="BLUEDART",
                confirm=False,
            )
        assert exc_info.value.code == "INVALID_SHIPMENT_STATUS"


@pytest.mark.asyncio
async def test_assign_courier_auth_required():
    with patch("tools.shipments.get_session", return_value=None), \
         patch("config.DEV_MODE", False):
        with pytest.raises(WriteError) as exc_info:
            await assign_courier(
                order_id="order-1082",
                courier_id="BLUEDART",
                confirm=False,
            )
        assert exc_info.value.code == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_assign_courier_unsupported_backend_capability(auth_session):
    unassigned_shipment = dict(SAMPLE_RAW_SHIPMENT, courierId=None, courierName=None)
    order_data = dict(SAMPLE_RAW_ORDER, shipments=[unassigned_shipment])
    api = build_mock_api(orders=[order_data])

    # Simulate backend returning 405 Method Not Allowed for courier assignment endpoint
    async def mock_unsupported_put(method: str, endpoint: str, **kwargs):
        req = httpx.Request(method, f"https://api.test{endpoint}")
        resp = httpx.Response(405, request=req)
        raise httpx.HTTPStatusError("Method Not Allowed", request=req, response=resp)

    api.async_request = AsyncMock(side_effect=mock_unsupported_put)

    with patch("tools.orders.get_api", return_value=api):
        preview = await assign_courier(
            order_id="order-1082",
            courier_id="BLUEDART",
            confirm=False,
        )
        token = preview["confirmation_token"]

        with pytest.raises(WriteError) as exc_info:
            await assign_courier(
                order_id="order-1082",
                courier_id="BLUEDART",
                confirm=True,
                confirmation_token=token,
            )
        assert exc_info.value.code == "UNSUPPORTED_OPERATION"
        assert "not support courier assignment" in exc_info.value.message.lower()
