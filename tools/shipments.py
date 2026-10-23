import copy
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any
from uuid import uuid4

from services.auth_session import get_session
from services.pointnxt_api import PointNXTAPI
from services.response_safety import sanitize_public_response
from services.timezone import local_timestamp, resolve_timezone
from services.validation import optional_id, optional_text, page_limit
from services.write_tools import (
    ConfirmationTokenManager,
    InMemoryWriteStateStore,
    WriteContext,
    WriteError,
    map_backend_error,
)
from tools import orders
from tools.orders import SUPPORTED_ORDER_STATUSES
from tools.status_options import SHIP_STATUSES

logger = logging.getLogger(__name__)


def get_api() -> PointNXTAPI:
    return orders.get_api()


async def get_order_by_id(order_id: str) -> dict[str, Any]:
    return await orders.get_order_by_id(order_id)


async def get_order(order_reference: str) -> dict[str, Any]:
    return await orders.get_order(order_reference)


async def get_orders(**kwargs) -> dict[str, Any]:
    return await orders.get_orders(**kwargs)

MAX_SHIPMENT_PAGE_SIZE = 100
SHIPMENT_SCAN_MAX_PAGES = 10
FULFILLMENT_QUEUE_STATUSES = {"PENDING", "CONFIRMED", "ON_HOLD", "PARTIALLY_FULFILLED"}
_DEFAULT_FALLBACK_SECRET = "pointnxt-default-write-confirmation-secret-32b"
_shipment_state_store = InMemoryWriteStateStore()


def _get_shipment_token_manager() -> ConfirmationTokenManager:
    from config import PNXT_CONFIRMATION_SECRET, PNXT_CONFIRMATION_TTL_SECONDS
    secret = (
        PNXT_CONFIRMATION_SECRET
        if PNXT_CONFIRMATION_SECRET and len(PNXT_CONFIRMATION_SECRET.encode()) >= 32
        else _DEFAULT_FALLBACK_SECRET
    )
    return ConfirmationTokenManager(secret, PNXT_CONFIRMATION_TTL_SECONDS)


def _parse_iso_datetime(val: Any) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    try:
        cleaned = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _extract_tracking_info(shipment: dict[str, Any]) -> dict[str, Any]:
    awb = (
        shipment.get("awb")
        or shipment.get("awbNo")
        or shipment.get("trackingNumber")
        or shipment.get("trackingNo")
    )
    courier = (
        shipment.get("courier")
        or shipment.get("courierName")
        or shipment.get("courierId")
        or shipment.get("carrier")
    )
    ship_status = shipment.get("shipStatus") or shipment.get("status")
    events = (
        shipment.get("trackingEvents")
        or shipment.get("events")
        or shipment.get("trackingHistory")
        or []
    )
    expected_delivery = (
        shipment.get("expectedDeliveryDate")
        or shipment.get("estimatedDeliveryDate")
        or shipment.get("expectedDelivery")
        or shipment.get("edd")
    )
    delivered_at = shipment.get("deliveredAt")
    tracking_url = shipment.get("trackingUrl") or shipment.get("trackingLink")

    return {
        "awb": awb,
        "courier": courier,
        "ship_status": ship_status,
        "expected_delivery": expected_delivery,
        "delivered_at": delivered_at,
        "tracking_url": tracking_url,
        "events": events if isinstance(events, list) else [],
    }


def _shipment_view(order: dict[str, Any]) -> dict[str, Any]:
    shipments = order.get("shipments")
    return {
        "order_id": order.get("id"),
        "order_number": order.get("orderNo"),
        "channel_order_number": order.get("channelOrderNo"),
        "order_date": order.get("orderDate"),
        "order_status": order.get("orderStatus"),
        "channel_order_status": order.get("channelOrderStatus"),
        "fulfillment_status": order.get("orderStatus"),
        "warehouse": order.get("warehouse"),
        "shipments": shipments if shipments is not None else [],
        "shipment_data_available": bool(shipments),
        "tracking_refresh": "not_performed",
    }


async def search_shipments(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    order_status: str | None = None,
    warehouse_id: str | None = None,
    channel_id: str | None = None,
    include_without_shipments: bool = False,
    ship_status: str | None = None,
) -> dict[str, Any]:
    """Find stored shipments derived from the order-list service."""
    page_limit(limit, page, MAX_SHIPMENT_PAGE_SIZE)
    optional_text(order_reference, "order_reference")
    optional_id(warehouse_id, "warehouse_id")
    optional_id(channel_id, "channel_id")
    optional_text(ship_status, "ship_status")
    if ship_status is not None and ship_status.upper() not in SHIP_STATUSES:
        raise ValueError(
            f"Invalid ship_status {ship_status!r}. Valid values: {', '.join(SHIP_STATUSES)}"
        )
    if order_status is not None and order_status.upper() not in SUPPORTED_ORDER_STATUSES:
        raise ValueError(
            f"Invalid order_status {order_status!r}. Valid values: "
            f"{', '.join(sorted(SUPPORTED_ORDER_STATUSES))}"
        )
    if include_without_shipments and ship_status is None:
        response = await get_orders(
            limit=limit,
            page=page,
            order_no=order_reference,
            order_status=order_status,
            warehouse_id=warehouse_id,
            channel_id=channel_id,
        )
        data = response.get("data", {})
        items = data.get("items", []) or []
        pagination = data.get("metadata", {})
        total_is_partial = False
    else:
        all_items: list[dict[str, Any]] = []
        source_page = 1
        scanned = 0
        source_total: int | None = None
        total_is_partial = False
        while source_page <= SHIPMENT_SCAN_MAX_PAGES:
            response = await get_orders(
                limit=MAX_SHIPMENT_PAGE_SIZE,
                page=source_page,
                order_no=order_reference,
                order_status=order_status,
                warehouse_id=warehouse_id,
                channel_id=channel_id,
            )
            data = response.get("data", {})
            source_items = data.get("items", []) or []
            scanned += len(source_items)
            metadata = data.get("metadata", {}) or {}
            total = metadata.get("totalItems", metadata.get("total"))
            if isinstance(total, int):
                source_total = total
            all_items.extend(
                order
                for order in source_items
                if order.get("shipments")
                and (
                    ship_status is None
                    or any(
                        str(shipment.get("shipStatus") or "").upper()
                        == ship_status.upper()
                        for shipment in order.get("shipments", [])
                    )
                )
            )
            if not source_items or (
                isinstance(source_total, int) and scanned >= source_total
            ) or (not isinstance(source_total, int) and len(source_items) < MAX_SHIPMENT_PAGE_SIZE):
                break
            if source_page == SHIPMENT_SCAN_MAX_PAGES:
                total_is_partial = True
                break
            source_page += 1
        total = len(all_items)
        start = (page - 1) * limit
        items = all_items[start : start + limit]
        pagination = {
            "page": page,
            "limit": limit,
            "totalItems": total,
            "in_page": len(items),
        }
    views = [_shipment_view(order) for order in items]
    if total_is_partial:
        pagination["total_is_partial"] = True
    return {
        "found": bool(views),
        "shipments": views,
        "pagination": pagination,
        "message": None if items else "No matching shipment records found.",
    }


async def get_order_shipments(order_reference: str) -> dict[str, Any]:
    """Find stored shipment information for a display or channel order number."""
    optional_text(order_reference, "order_reference", required=True)
    reference = order_reference.strip().lstrip("#")
    result = await search_shipments(limit=1, page=1, order_reference=reference)
    shipments = result["shipments"]
    if not shipments:
        return {"found": False, "shipment": None, "message": "No matching order found."}
    return {
        "found": True,
        "shipment": shipments[0],
        "message": "Tracking data is stored backend data; no live refresh was performed.",
    }


async def get_fulfillment_queue(
    limit: int = 20,
    page: int = 1,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """Return orders in statuses that represent pending fulfillment work."""
    page_limit(limit, page, MAX_SHIPMENT_PAGE_SIZE)
    optional_id(warehouse_id, "warehouse_id")
    response = await get_orders(
        limit=limit,
        page=page,
        warehouse_id=warehouse_id,
        order_status="PENDING",
    )
    data = response.get("data", {})
    items = data.get("items", []) or []
    # The backend currently exposes order status, not separate picking/packing stages.
    return {
        "found": bool(items),
        "work_queue_supported": False,
        "orders": [
            {
                "order_id": order.get("id"),
                "order_number": order.get("orderNo"),
                "order_date": order.get("orderDate") or order.get("createdAt"),
                "status": order.get("orderStatus"),
                "warehouse": order.get("warehouse"),
                "shipments": order.get("shipments", []),
            }
            for order in items
        ],
        "pagination": data.get("metadata", {}),
        "message": "Separate picking/packing work-queue data is not exposed by the current backend.",
    }


async def get_shipment_tracking(
    order_reference: str | None = None,
    shipment_id: str | None = None,
    awb: str | None = None,
) -> dict[str, Any]:
    """Retrieve stored tracking details for a specific shipment or order reference.

    Returns stored carrier tracking details without performing an external live refresh.
    Clearly distinguishes between unavailable tracking information and backend request errors.
    """
    if not order_reference and not shipment_id and not awb:
        raise ValueError("At least one of order_reference, shipment_id, or awb must be provided")

    resolved_order: dict[str, Any] | None = None
    target_shipment: dict[str, Any] | None = None

    if order_reference and isinstance(order_reference, str) and order_reference.strip():
        ref = order_reference.strip().lstrip("#")
        # 1. Search by order_no first
        try:
            search_res = await get_orders(limit=1, page=1, order_no=ref)
            items = search_res.get("data", {}).get("items", []) or []
            if items and items[0].get("id"):
                raw_res = await get_order_by_id(str(items[0]["id"]))
                resolved_order = raw_res.get("data", raw_res) if isinstance(raw_res, dict) else raw_res
        except Exception as exc:
            status_code = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status_code in (404, None) and not isinstance(exc, (httpx.HTTPStatusError, httpx.RequestError)):
                resolved_order = None
            elif status_code == 404:
                resolved_order = None
            else:
                raise

        # 2. Try direct get_order_by_id(ref)
        if not resolved_order:
            try:
                raw_res = await get_order_by_id(ref)
                data = raw_res.get("data", raw_res) if isinstance(raw_res, dict) else {}
                if data and data.get("id"):
                    resolved_order = data
            except Exception as exc:
                status_code = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if status_code == 404:
                    resolved_order = None
                else:
                    raise

        if not resolved_order:
            return {
                "found": False,
                "order_reference": ref,
                "shipment": None,
                "tracking": None,
                "message": f"Order {ref!r} not found.",
            }

        shipments = resolved_order.get("shipments") or []
        if not shipments:
            result = {
                "found": True,
                "order_id": resolved_order.get("id"),
                "order_number": resolved_order.get("orderNo"),
                "shipment_id": None,
                "tracking_available": False,
                "tracking_status": "NO_SHIPMENTS",
                "tracking_refresh": "not_performed",
                "note": "Order exists, but has no recorded shipments.",
                "tracking": None,
                "shipment": None,
                "message": f"Order {resolved_order.get('orderNo') or ref} has no recorded shipment records.",
            }
            return sanitize_public_response(result)

        if shipment_id:
            target_shipment = next(
                (s for s in shipments if str(s.get("id") or s.get("shipmentId")) == str(shipment_id).strip()),
                None,
            )
            if not target_shipment:
                return {
                    "found": False,
                    "order_id": resolved_order.get("id"),
                    "order_number": resolved_order.get("orderNo"),
                    "shipment_id": shipment_id,
                    "shipment": None,
                    "tracking": None,
                    "message": f"Shipment {shipment_id!r} not found on order {resolved_order.get('orderNo')}.",
                }
        elif awb:
            target_shipment = next(
                (s for s in shipments if str(s.get("awb") or s.get("awbNo") or s.get("trackingNumber")) == str(awb).strip()),
                None,
            )
            if not target_shipment:
                return {
                    "found": False,
                    "order_id": resolved_order.get("id"),
                    "order_number": resolved_order.get("orderNo"),
                    "awb": awb,
                    "shipment": None,
                    "tracking": None,
                    "message": f"AWB {awb!r} not found on order {resolved_order.get('orderNo')}.",
                }
        else:
            target_shipment = shipments[0]
    else:
        target_s_id = str(shipment_id).strip() if shipment_id else None
        target_awb = str(awb).strip() if awb else None

        source_page = 1
        while source_page <= SHIPMENT_SCAN_MAX_PAGES and not target_shipment:
            resp = await get_orders(limit=MAX_SHIPMENT_PAGE_SIZE, page=source_page)
            orders = resp.get("data", {}).get("items", []) or []
            if not orders:
                break
            for o in orders:
                for s in (o.get("shipments") or []):
                    s_id = str(s.get("id") or s.get("shipmentId") or "")
                    s_awb = str(s.get("awb") or s.get("awbNo") or s.get("trackingNumber") or "")
                    if (target_s_id and s_id == target_s_id) or (target_awb and s_awb == target_awb):
                        resolved_order = o
                        target_shipment = s
                        break
                if target_shipment:
                    break
            source_page += 1

        if not target_shipment or not resolved_order:
            return {
                "found": False,
                "shipment_id": shipment_id,
                "awb": awb,
                "shipment": None,
                "tracking": None,
                "message": f"No shipment matching shipment_id={shipment_id!r} or awb={awb!r} was found.",
            }

    tracking_data = _extract_tracking_info(target_shipment)
    has_awb = bool(tracking_data["awb"] and str(tracking_data["awb"]).strip().upper() not in ("N/A", "NONE", "PENDING", "NULL"))
    has_events = bool(tracking_data["events"] and len(tracking_data["events"]) > 0)

    tracking_available = bool(has_awb or has_events)
    if tracking_available:
        tracking_status = "AVAILABLE"
        msg = "Stored tracking details retrieved successfully; no live carrier refresh was performed."
    else:
        tracking_status = "UNAVAILABLE"
        msg = "Shipment record exists, but tracking information (AWB or tracking events) is not currently available from the carrier/backend."

    result = {
        "found": True,
        "order_id": resolved_order.get("id"),
        "order_number": resolved_order.get("orderNo"),
        "shipment_id": target_shipment.get("id") or target_shipment.get("shipmentId"),
        "tracking_available": tracking_available,
        "tracking_status": tracking_status,
        "tracking_refresh": "not_performed",
        "note": "Tracking data reflects stored backend records; live carrier tracking was not refreshed.",
        "tracking": tracking_data,
        "shipment": target_shipment,
        "message": msg,
    }
    return sanitize_public_response(result)


async def find_delayed_shipments(
    threshold_days: float = 7.0,
    threshold_hours: float | None = None,
    warehouse_id: str | None = None,
    channel_id: str | None = None,
    timezone: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Identify shipments whose expected delivery dates or transit-time thresholds have been exceeded.

    Uses actual expected delivery dates when available. Otherwise falls back to an explicitly
    configurable transit-time threshold. Never classifies a shipment as delayed solely because
    tracking updates are missing.
    """
    page_limit(limit, page, MAX_SHIPMENT_PAGE_SIZE)
    optional_id(warehouse_id, "warehouse_id")
    optional_id(channel_id, "channel_id")

    if threshold_hours is not None:
        if not isinstance(threshold_hours, (int, float)) or threshold_hours <= 0:
            raise ValueError("threshold_hours must be a positive number")
        effective_threshold_hours = float(threshold_hours)
    else:
        if not isinstance(threshold_days, (int, float)) or threshold_days <= 0:
            raise ValueError("threshold_days must be a positive number")
        effective_threshold_hours = float(threshold_days) * 24.0

    timezone_name, _ = resolve_timezone(timezone)
    now_utc = datetime.now(dt_timezone.utc)

    delayed_shipments: list[dict[str, Any]] = []
    on_track_shipments: list[dict[str, Any]] = []

    source_page = 1
    scanned_orders = 0
    while source_page <= SHIPMENT_SCAN_MAX_PAGES:
        resp = await get_orders(
            limit=MAX_SHIPMENT_PAGE_SIZE,
            page=source_page,
            warehouse_id=warehouse_id,
            channel_id=channel_id,
        )
        orders_data = resp.get("data", {})
        orders = orders_data.get("items", []) or []
        if not orders:
            break
        scanned_orders += len(orders)
        metadata = orders_data.get("metadata", {}) or {}
        total = metadata.get("totalItems", metadata.get("total"))

        for order in orders:
            order_status = str(order.get("orderStatus") or "").upper()
            if order_status in ("DELIVERED", "CLOSED", "CANCELLED"):
                continue

            for shipment in (order.get("shipments") or []):
                ship_status = str(shipment.get("shipStatus") or "").upper()
                if ship_status in ("DELIVERED", "CANCELLED", "RETURNED") or shipment.get("deliveredAt"):
                    continue

                expected_raw = (
                    shipment.get("expectedDeliveryDate")
                    or shipment.get("estimatedDeliveryDate")
                    or shipment.get("expectedDelivery")
                    or shipment.get("edd")
                )
                expected_dt = _parse_iso_datetime(expected_raw)

                origin_raw = (
                    shipment.get("shippedAt")
                    or shipment.get("dispatchedAt")
                    or shipment.get("createdAt")
                    or order.get("orderDate")
                    or order.get("createdAt")
                )
                origin_dt = _parse_iso_datetime(origin_raw)

                is_delayed = False
                delay_basis: str | None = None
                delay_hours = 0.0
                transit_hours = round((now_utc - origin_dt).total_seconds() / 3600.0, 2) if origin_dt else None
                fallback_used = False

                if expected_dt is not None:
                    delay_basis = "EXPECTED_DELIVERY_DATE"
                    if now_utc > expected_dt:
                        is_delayed = True
                        delay_hours = round((now_utc - expected_dt).total_seconds() / 3600.0, 2)
                        delay_desc = f"Exceeded expected delivery date ({expected_dt.isoformat()}) by {delay_hours:.1f} hours"
                    else:
                        is_delayed = False
                        delay_desc = "Within expected delivery window"
                elif origin_dt is not None:
                    fallback_used = True
                    delay_basis = "TRANSIT_THRESHOLD_FALLBACK"
                    if transit_hours is not None and transit_hours > effective_threshold_hours:
                        is_delayed = True
                        delay_hours = round(transit_hours - effective_threshold_hours, 2)
                        delay_desc = (
                            f"Transit duration ({transit_hours:.1f}h) exceeded configurable fallback "
                            f"threshold of {effective_threshold_hours:.1f}h ({threshold_days} days) "
                            f"because no expectedDeliveryDate was provided by carrier."
                        )
                    else:
                        is_delayed = False
                        delay_desc = "Within transit threshold window"
                else:
                    is_delayed = False
                    delay_basis = "INSUFFICIENT_TIMESTAMPS"
                    delay_desc = "Timestamps unavailable to determine delivery delay; tracking updates alone do not mark delayed."

                events = shipment.get("trackingEvents") or shipment.get("events") or []
                item = {
                    "order_id": order.get("id"),
                    "order_number": order.get("orderNo"),
                    "shipment_id": shipment.get("id") or shipment.get("shipmentId"),
                    "awb": shipment.get("awb") or shipment.get("awbNo") or shipment.get("trackingNumber"),
                    "courier": shipment.get("courier") or shipment.get("courierName") or shipment.get("courierId"),
                    "ship_status": ship_status or "UNKNOWN",
                    "order_status": order_status,
                    "is_delayed": is_delayed,
                    "delay_basis": delay_basis,
                    "delay_hours": delay_hours,
                    "transit_hours": transit_hours,
                    "description": delay_desc,
                    "expected_delivery_date": expected_dt.isoformat() if expected_dt else None,
                    "expected_delivery_local": local_timestamp(expected_dt.isoformat(), timezone_name) if expected_dt else None,
                    "origin_timestamp": origin_dt.isoformat() if origin_dt else None,
                    "origin_timestamp_local": local_timestamp(origin_dt.isoformat(), timezone_name) if origin_dt else None,
                    "fallback_logic_used": fallback_used,
                    "tracking_events_count": len(events) if isinstance(events, list) else 0,
                }

                if is_delayed:
                    delayed_shipments.append(item)
                else:
                    on_track_shipments.append(item)

        if (isinstance(total, int) and scanned_orders >= total) or len(orders) < MAX_SHIPMENT_PAGE_SIZE:
            break
        source_page += 1

    start = (page - 1) * limit
    paginated_delayed = delayed_shipments[start : start + limit]

    result = {
        "found": bool(delayed_shipments),
        "delayed_count": len(delayed_shipments),
        "on_track_count": len(on_track_shipments),
        "total_evaluated": len(delayed_shipments) + len(on_track_shipments),
        "threshold_days": threshold_days,
        "effective_threshold_hours": effective_threshold_hours,
        "fallback_logic_description": (
            f"When expectedDeliveryDate is unavailable, active shipments in transit for more than "
            f"{effective_threshold_hours:.1f} hours ({threshold_days} days) are marked delayed."
        ),
        "timezone": timezone_name,
        "pagination": {
            "page": page,
            "limit": limit,
            "totalItems": len(delayed_shipments),
            "in_page": len(paginated_delayed),
        },
        "delayed_shipments": paginated_delayed,
        "on_track_shipments": on_track_shipments[:limit],
        "message": (
            f"Found {len(delayed_shipments)} delayed shipment(s) exceeding delivery deadlines."
            if delayed_shipments
            else f"No delayed shipments found exceeding thresholds ({len(on_track_shipments)} evaluated shipment(s) are on-track)."
        ),
    }
    return sanitize_public_response(result)


async def find_untracked_shipments(
    warehouse_id: str | None = None,
    channel_id: str | None = None,
    ship_status: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Identify shipments with missing or unusable tracking information.

    Distinguishes missing tracking numbers, missing courier assignments, and absent
    tracking events where backend records support these distinctions.
    """
    page_limit(limit, page, MAX_SHIPMENT_PAGE_SIZE)
    optional_id(warehouse_id, "warehouse_id")
    optional_id(channel_id, "channel_id")
    optional_text(ship_status, "ship_status")

    if ship_status is not None and ship_status.upper() not in SHIP_STATUSES:
        raise ValueError(
            f"Invalid ship_status {ship_status!r}. Valid values: {', '.join(SHIP_STATUSES)}"
        )

    untracked_shipments: list[dict[str, Any]] = []
    tracked_shipments: list[dict[str, Any]] = []

    source_page = 1
    scanned_orders = 0
    while source_page <= SHIPMENT_SCAN_MAX_PAGES:
        resp = await get_orders(
            limit=MAX_SHIPMENT_PAGE_SIZE,
            page=source_page,
            warehouse_id=warehouse_id,
            channel_id=channel_id,
        )
        orders_data = resp.get("data", {})
        orders = orders_data.get("items", []) or []
        if not orders:
            break
        scanned_orders += len(orders)
        metadata = orders_data.get("metadata", {}) or {}
        total = metadata.get("totalItems", metadata.get("total"))

        for order in orders:
            order_status = str(order.get("orderStatus") or "").upper()
            if order_status in ("CANCELLED", "CLOSED"):
                continue

            for shipment in (order.get("shipments") or []):
                s_status = str(shipment.get("shipStatus") or "").upper()
                if ship_status and s_status != ship_status.upper():
                    continue
                if s_status in ("CANCELLED", "RETURNED"):
                    continue

                awb = (
                    shipment.get("awb")
                    or shipment.get("awbNo")
                    or shipment.get("trackingNumber")
                    or shipment.get("trackingNo")
                )
                courier = (
                    shipment.get("courier")
                    or shipment.get("courierName")
                    or shipment.get("courierId")
                    or shipment.get("carrier")
                )
                events = (
                    shipment.get("trackingEvents")
                    or shipment.get("events")
                    or shipment.get("trackingHistory")
                    or []
                )

                missing_tracking_number = (
                    not awb
                    or not str(awb).strip()
                    or str(awb).strip().upper() in ("N/A", "NONE", "PENDING", "NULL", "UNASSIGNED")
                )
                missing_courier = (
                    not courier
                    or not str(courier).strip()
                    or str(courier).strip().upper() in ("N/A", "NONE", "PENDING", "NULL", "UNASSIGNED")
                )
                absent_tracking_events = (
                    not missing_tracking_number
                    and not missing_courier
                    and (not events or len(events) == 0)
                )

                tracking_issues: list[str] = []
                if missing_tracking_number:
                    tracking_issues.append("MISSING_TRACKING_NUMBER")
                if missing_courier:
                    tracking_issues.append("MISSING_COURIER_ASSIGNMENT")
                if absent_tracking_events:
                    tracking_issues.append("ABSENT_TRACKING_EVENTS")

                item = {
                    "order_id": order.get("id"),
                    "order_number": order.get("orderNo"),
                    "shipment_id": shipment.get("id") or shipment.get("shipmentId"),
                    "ship_status": s_status or "UNKNOWN",
                    "awb": awb if not missing_tracking_number else None,
                    "courier": courier if not missing_courier else None,
                    "tracking_issues": tracking_issues,
                    "untracked_category": (
                        "MISSING_COURIER_AND_TRACKING_NUMBER"
                        if missing_tracking_number and missing_courier
                        else "MISSING_COURIER_ASSIGNMENT"
                        if missing_courier
                        else "MISSING_TRACKING_NUMBER"
                        if missing_tracking_number
                        else "ABSENT_TRACKING_EVENTS"
                        if absent_tracking_events
                        else "TRACKED"
                    ),
                    "events_count": len(events) if isinstance(events, list) else 0,
                    "created_at": shipment.get("createdAt"),
                }

                if tracking_issues:
                    untracked_shipments.append(item)
                else:
                    tracked_shipments.append(item)

        if (isinstance(total, int) and scanned_orders >= total) or len(orders) < MAX_SHIPMENT_PAGE_SIZE:
            break
        source_page += 1

    start = (page - 1) * limit
    paginated_untracked = untracked_shipments[start : start + limit]

    missing_awb_count = sum(1 for s in untracked_shipments if "MISSING_TRACKING_NUMBER" in s["tracking_issues"])
    missing_courier_count = sum(1 for s in untracked_shipments if "MISSING_COURIER_ASSIGNMENT" in s["tracking_issues"])
    absent_events_count = sum(1 for s in untracked_shipments if "ABSENT_TRACKING_EVENTS" in s["tracking_issues"])

    result = {
        "found": bool(untracked_shipments),
        "total_untracked": len(untracked_shipments),
        "total_tracked": len(tracked_shipments),
        "total_evaluated": len(untracked_shipments) + len(tracked_shipments),
        "summary_by_issue": {
            "missing_tracking_number_count": missing_awb_count,
            "missing_courier_count": missing_courier_count,
            "absent_tracking_events_count": absent_events_count,
        },
        "pagination": {
            "page": page,
            "limit": limit,
            "totalItems": len(untracked_shipments),
            "in_page": len(paginated_untracked),
        },
        "untracked_shipments": paginated_untracked,
        "message": (
            f"Found {len(untracked_shipments)} shipment(s) with missing or unusable tracking data."
            if untracked_shipments
            else "All evaluated shipments have complete tracking information."
        ),
    }
    return sanitize_public_response(result)


async def assign_courier(
    order_id: str | None = None,
    order_reference: str | None = None,
    shipment_id: str | None = None,
    courier_id: str | None = None,
    courier_name: str | None = None,
    service_type: str | None = None,
    reason: str | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Assign an eligible courier to a shipment through the actual backend-supported workflow.

    Enforces authentication, shipment existence, courier eligibility, two-phase confirmation,
    concurrency control, and duplicate-assignment idempotency. If courier assignment is unsupported
    by the backend, reports the exact missing capability without simulating success.
    """
    if not order_id and not order_reference:
        raise ValueError("Either order_id or order_reference must be provided")

    if not courier_id and not courier_name:
        raise ValueError("Either courier_id or courier_name must be provided")

    clean_courier_id = courier_id.strip() if courier_id and isinstance(courier_id, str) else None
    clean_courier_name = courier_name.strip() if courier_name and isinstance(courier_name, str) else None
    courier_key = clean_courier_id or clean_courier_name
    if not courier_key:
        raise ValueError("A non-empty courier_id or courier_name is required")
    if len(courier_key) > 100:
        raise ValueError("Courier identifier must be at most 100 characters")
    if re.search(r"[\x00-\x1f\x7f]", courier_key):
        raise ValueError("Courier identifier contains invalid control characters")

    clean_reason: str | None = None
    if reason is not None:
        if not isinstance(reason, str):
            raise ValueError("reason must be a string")
        clean_reason = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", reason).strip()
        if len(clean_reason) > 1000:
            raise ValueError("reason must be at most 1000 characters")

    clean_service_type: str | None = None
    if service_type is not None:
        if not isinstance(service_type, str):
            raise ValueError("service_type must be a string")
        clean_service_type = service_type.strip().upper()
        if len(clean_service_type) > 50:
            raise ValueError("service_type must be at most 50 characters")

    # 1. Authentication & Authorization
    session = get_session()
    is_auth = session and session.is_authenticated()
    from config import DEV_MODE, POINTNXT_ACCESS_TOKEN, POINTNXT_TENANT_ID
    from services.request_context import get_request_key
    dev_auth = DEV_MODE and get_request_key() == "stdio" and bool(POINTNXT_ACCESS_TOKEN and POINTNXT_TENANT_ID)
    if not is_auth and not dev_auth:
        raise WriteError("AUTH_REQUIRED", "Sign in to PointNXT before assigning courier to a shipment.")

    tenant_id = (session.get_tenant_id() if session else None) or POINTNXT_TENANT_ID or "default-tenant"
    user_id = (session.user_id or session.email if session else None) or "dev-user"

    # 2. Resolve order & shipment
    resolved_id: str | None = None
    if order_id and isinstance(order_id, str) and order_id.strip():
        resolved_id = order_id.strip()
    elif order_reference and isinstance(order_reference, str) and order_reference.strip():
        reference = order_reference.strip().lstrip("#")
        search = await get_orders(limit=1, page=1, order_no=reference)
        items = search.get("data", {}).get("items", []) or []
        if items and items[0].get("id"):
            resolved_id = str(items[0]["id"])
        else:
            resolved_id = reference

    if not resolved_id:
        raise ValueError("Could not resolve a valid order ID")

    try:
        raw_detail = await get_order_by_id(resolved_id)
    except Exception as exc:
        status_code = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        if status_code == 404:
            try:
                search = await get_orders(limit=1, page=1, order_no=resolved_id)
                items = search.get("data", {}).get("items", []) or []
                if items and items[0].get("id"):
                    resolved_id = str(items[0]["id"])
                    raw_detail = await get_order_by_id(resolved_id)
                else:
                    raise WriteError("NOT_FOUND", f"Order {resolved_id!r} does not exist.") from exc
            except WriteError:
                raise
            except Exception:
                raise WriteError("NOT_FOUND", f"Order {resolved_id!r} does not exist.") from exc
        else:
            raise map_backend_error(exc, str(uuid4())) from exc

    order = raw_detail.get("data", raw_detail) if isinstance(raw_detail, dict) else {}
    if not order or not isinstance(order, dict) or not order.get("id"):
        raise WriteError("NOT_FOUND", f"Order {resolved_id!r} does not exist.")

    shipments = order.get("shipments") or []
    if not shipments:
        raise WriteError(
            "SHIPMENT_NOT_FOUND",
            f"Order {order.get('orderNo') or resolved_id} has no existing shipment records to assign courier to.",
        )

    target_shipment: dict[str, Any] | None = None
    if shipment_id and isinstance(shipment_id, str) and shipment_id.strip():
        target_shipment = next(
            (s for s in shipments if str(s.get("id") or s.get("shipmentId")) == shipment_id.strip()),
            None,
        )
        if not target_shipment:
            raise WriteError(
                "SHIPMENT_NOT_FOUND",
                f"Shipment {shipment_id!r} was not found on order {order.get('orderNo') or resolved_id}.",
            )
    else:
        if len(shipments) == 1:
            target_shipment = shipments[0]
        else:
            raise WriteError(
                "AMBIGUOUS_SHIPMENT",
                f"Order {order.get('orderNo') or resolved_id} has {len(shipments)} shipments. "
                f"Please provide shipment_id to specify which shipment to assign.",
            )

    shipment_target_id = str(target_shipment.get("id") or target_shipment.get("shipmentId") or "shipment-1")
    ship_status = str(target_shipment.get("shipStatus") or "").upper()
    order_no = order.get("orderNo") or resolved_id

    if ship_status in ("DELIVERED", "SHIPPED", "IN_TRANSIT", "CANCELLED"):
        raise WriteError(
            "INVALID_SHIPMENT_STATUS",
            f"Cannot assign courier to shipment {shipment_target_id} in status {ship_status!r}. "
            f"Couriers can only be assigned to shipments in pending or unfulfilled stages (e.g. CREATED, READY_FOR_PICKUP).",
            details={"shipment_id": shipment_target_id, "current_ship_status": ship_status},
        )

    curr_c_id = str(target_shipment.get("courierId") or "").strip().upper()
    curr_c_name = str(target_shipment.get("courierName") or target_shipment.get("courier") or "").strip().upper()
    req_c_upper = courier_key.upper()
    is_duplicate = (curr_c_id and curr_c_id == req_c_upper) or (curr_c_name and curr_c_name == req_c_upper)

    target_version = str(order.get("version") or order.get("updatedAt") or target_shipment.get("updatedAt") or ship_status)

    token_manager = _get_shipment_token_manager()
    args_for_hash = {
        "order_id": resolved_id,
        "shipment_id": shipment_target_id,
        "courier_id": clean_courier_id,
        "courier_name": clean_courier_name,
        "service_type": clean_service_type,
        "reason": clean_reason,
    }
    args_hash = hashlib.sha256(
        json.dumps(args_for_hash, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    # 3. Preview phase (confirm=False)
    if not confirm:
        fake_spec = type("Spec", (), {"name": "assign_courier"})()
        fake_context = WriteContext(str(user_id), str(tenant_id))
        target_info = {"id": f"{resolved_id}:{shipment_target_id}", "version": target_version}
        token, token_payload = token_manager.issue(fake_spec, args_for_hash, target_info, fake_context)

        warning = (
            f"Shipment {shipment_target_id} already has courier {target_shipment.get('courierName') or target_shipment.get('courierId')} assigned."
            if is_duplicate
            else None
        )

        preview_result = {
            "status": "preview",
            "action": "assign_courier",
            "order_id": resolved_id,
            "order_number": order_no,
            "shipment_id": shipment_target_id,
            "current_ship_status": ship_status,
            "current_courier": target_shipment.get("courierName") or target_shipment.get("courierId"),
            "requested_courier_id": clean_courier_id,
            "requested_courier_name": clean_courier_name,
            "service_type": clean_service_type,
            "reason": clean_reason,
            "target_version": target_version,
            "warning": warning,
            "duplicate_assignment": is_duplicate,
            "summary": f"Preview: Assign courier {clean_courier_name or clean_courier_id} to shipment {shipment_target_id} of order {order_no}",
            "confirmation_token": token,
            "expires_at": datetime.fromtimestamp(token_payload["expires_at"], dt_timezone.utc).isoformat(),
            "instructions": "To commit this courier assignment, call assign_courier with confirm=True and confirmation_token.",
        }
        return sanitize_public_response({k: v for k, v in preview_result.items() if v is not None})

    # 4. Commit phase (confirm=True)
    if not confirmation_token or not isinstance(confirmation_token, str):
        raise WriteError(
            "CONFIRMATION_REQUIRED",
            "Set confirm=true and provide the confirmation_token returned by preview. Run with confirm=false first to preview.",
        )

    if idempotency_key:
        cached = _shipment_state_store.get_idempotency(str(user_id), "assign_courier", idempotency_key)
        if cached:
            if cached.get("args_hash") != args_hash:
                raise WriteError("IDEMPOTENCY_CONFLICT", "The idempotency key was used with different arguments.")
            return copy.deepcopy(cached["result"])

    token_payload = token_manager.verify(confirmation_token)
    if token_payload.get("tool") != "assign_courier":
        raise WriteError("TOKEN_INVALID", "The confirmation token does not belong to this write tool.")
    if token_payload.get("user_id") != str(user_id) or token_payload.get("tenant_id") != str(tenant_id):
        raise WriteError("TOKEN_INVALID", "The confirmation token does not belong to the current user or tenant.")

    if not _shipment_state_store.consume_nonce(token_payload["nonce"], token_payload["expires_at"]):
        raise WriteError("TOKEN_USED", "The confirmation token has already been used.")

    if token_payload.get("args_hash") != args_hash:
        raise WriteError("ARGS_CHANGED", "Arguments changed since preview; generate a new preview.")
    expected_target_id = f"{resolved_id}:{shipment_target_id}"
    if token_payload.get("target_id") != expected_target_id:
        raise WriteError("RECORD_CHANGED", "Target shipment changed since preview; generate a new preview.")
    if token_payload.get("target_version") != target_version:
        raise WriteError(
            "RECORD_CHANGED",
            f"The shipment or order was modified concurrently since preview (was version {token_payload.get('target_version')}, now {target_version}). Generate a new preview.",
        )

    if is_duplicate:
        audit_id = str(uuid4())
        committed = {
            "status": "committed",
            "order_id": resolved_id,
            "order_number": order_no,
            "shipment_id": shipment_target_id,
            "courier_id": clean_courier_id,
            "courier_name": clean_courier_name,
            "duplicate_assignment": True,
            "idempotent": True,
            "audit_id": audit_id,
            "message": f"Courier {clean_courier_name or clean_courier_id} is already assigned to shipment {shipment_target_id}. No write needed.",
        }
        sanitized_committed = sanitize_public_response(committed)
        key = idempotency_key or token_payload["nonce"]
        _shipment_state_store.put_idempotency(
            str(user_id), "assign_courier", key, args_hash, copy.deepcopy(sanitized_committed), time.time() + 86400
        )
        return sanitized_committed

    endpoint = f"/commerce/orders/{resolved_id}/shipments/{shipment_target_id}/courier"
    payload = {
        "courierId": clean_courier_id or clean_courier_name,
        "courierName": clean_courier_name or clean_courier_id,
        "serviceType": clean_service_type,
        "reason": clean_reason,
    }
    started = time.perf_counter()
    api = get_api()
    try:
        write_result = await api.async_request("PUT", endpoint, json=payload, retry=False)
    except Exception as error:
        status_code = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        if status_code in (404, 405):
            fallback_endpoint = f"/commerce/orders/{resolved_id}/shipments/{shipment_target_id}"
            try:
                write_result = await api.async_request("PUT", fallback_endpoint, json=payload, retry=False)
            except Exception as second_err:
                second_code = getattr(second_err, "status_code", None) or getattr(
                    getattr(second_err, "response", None), "status_code", None
                )
                if second_code in (404, 405, 501):
                    raise WriteError(
                        "UNSUPPORTED_OPERATION",
                        f"The PointNXT backend does not support courier assignment via API (endpoint returned HTTP {second_code}). "
                        f"Direct courier assignment must be configured through carrier integration or the PointNXT merchant portal.",
                        details={"order_id": resolved_id, "shipment_id": shipment_target_id, "http_status": second_code},
                    ) from second_err
                raise map_backend_error(second_err, str(uuid4())) from second_err
        elif status_code == 501:
            raise WriteError(
                "UNSUPPORTED_OPERATION",
                "The PointNXT backend does not support courier assignment via API (HTTP 501 Not Implemented).",
                details={"order_id": resolved_id, "shipment_id": shipment_target_id, "http_status": 501},
            ) from error
        else:
            raise map_backend_error(error, str(uuid4())) from error

    audit_id = str(uuid4())
    committed = {
        "status": "committed",
        "order_id": resolved_id,
        "order_number": order_no,
        "shipment_id": shipment_target_id,
        "courier_id": clean_courier_id,
        "courier_name": clean_courier_name,
        "service_type": clean_service_type,
        "reason": clean_reason,
        "audit_id": audit_id,
        "backend_response": write_result.get("data", write_result) if isinstance(write_result, dict) else write_result,
        "updated_at": datetime.now(dt_timezone.utc).isoformat(),
        "message": f"Successfully assigned courier {clean_courier_name or clean_courier_id} to shipment {shipment_target_id} of order {order_no}.",
    }

    sanitized_committed = sanitize_public_response(committed)
    key = idempotency_key or token_payload["nonce"]
    _shipment_state_store.put_idempotency(
        str(user_id), "assign_courier", key, args_hash, copy.deepcopy(sanitized_committed), time.time() + 86400
    )

    logger.info(
        "courier_assignment_committed",
        extra={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "order_id": resolved_id,
            "shipment_id": shipment_target_id,
            "courier": courier_key,
            "audit_id": audit_id,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return sanitized_committed

