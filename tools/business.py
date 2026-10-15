"""Read-only business-assistant summaries composed from existing tools."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from services.timezone import local_date_period, local_today, resolve_timezone
from services.validation import optional_date
from tools.orders import get_order_stats, get_orders
from tools.products import get_product_summary
from tools.returns import search_returns
from tools.shipments import (
    SHIPMENT_SCAN_MAX_PAGES,
    get_fulfillment_queue,
    search_shipments,
)

UTC_ZONE = timezone.utc

SHIPMENT_FOLLOW_UP_MIN_AGE_DAYS = 3


def _items(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("data", {}).get("items", []) or []


def _order_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    total = 0
    for order in orders:
        status = order.get("orderStatus") or "UNKNOWN"
        statuses[status] = statuses.get(status, 0) + 1
        total += order.get("grandTotal", order.get("orderTotal", 0)) or 0
    return {"count": len(orders), "status_counts": statuses, "order_value": total}


def _pagination_total(result: dict[str, Any], fallback: int) -> int:
    metadata = result.get("pagination") or result.get("data", {}).get("metadata", {})
    total = metadata.get("totalItems", metadata.get("total"))
    return total if isinstance(total, int) else fallback


def _evidence(
    records: list[dict[str, Any]],
    total_count: int,
    field: str,
    sort_key=None,
    total_count_is_partial: bool = False,
) -> dict[str, Any]:
    ordered_records = sorted(records, key=sort_key or _record_date)
    samples = [record.get(field) for record in ordered_records if record.get(field)][:10]
    return {
        "count": total_count,
        "total_count": total_count,
        "sample_size": len(samples),
        "is_sample": total_count > len(samples),
        "total_count_is_partial": total_count_is_partial,
        "orders": samples,
    }


def _record_date(record: dict[str, Any]) -> str:
    return (
        record.get("orderDate")
        or record.get("order_date")
        or record.get("createdAt")
        or record.get("requested_at")
        or ""
    )


def _shipment_date(order: dict[str, Any]) -> str:
    dates = [
        shipment.get("createdAt")
        for shipment in order.get("shipments", []) or []
        if shipment.get("createdAt")
    ]
    return min(dates) if dates else _record_date(order)


def _undelivered_shipment(order: dict[str, Any], now: datetime) -> bool:
    if order.get("order_status") == "PENDING":
        return False
    return any(
        shipment.get("deliveredAt") in (None, "")
        and str(shipment.get("shipStatus") or "").upper() != "DELIVERED"
        and _shipment_is_older_than(
            shipment.get("createdAt"), now, SHIPMENT_FOLLOW_UP_MIN_AGE_DAYS
        )
        for shipment in order.get("shipments", []) or []
    )


def _shipment_is_older_than(
    created_at: str | None, now: datetime, age_days: int
) -> bool:
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC_ZONE)
    return created < now - timedelta(days=age_days)


async def _shipment_follow_up() -> dict[str, Any]:
    """Collect stored, undelivered shipments without the fulfillment queue."""
    page = 1
    results: list[dict[str, Any]] = []
    scanned_items = 0
    now = datetime.now(UTC_ZONE)
    total_count_is_partial = False
    while page <= SHIPMENT_SCAN_MAX_PAGES:
        result = await search_shipments(limit=100, page=page)
        if (result.get("pagination") or {}).get("total_is_partial"):
            total_count_is_partial = True
        shipments = result.get("shipments", []) or []
        scanned_items += len(shipments)
        results.extend(
            order for order in shipments if _undelivered_shipment(order, now)
        )
        metadata = result.get("pagination") or {}
        total = metadata.get("totalItems", metadata.get("total"))
        if not shipments or (
            isinstance(total, int)
            and scanned_items >= total
        ) or (not isinstance(total, int) and len(shipments) < 100):
            return {
                "items": results,
                "total_count_is_partial": total_count_is_partial,
            }
        if page == SHIPMENT_SCAN_MAX_PAGES:
            total_count_is_partial = True
            break
        page += 1
    return {"items": results, "total_count_is_partial": total_count_is_partial}


async def _safe_call(name: str, operation):
    try:
        return name, await operation, None
    except Exception as error:  # noqa: BLE001
        message = str(error).lower()
        if "auth" in message or "sign in" in message or "401" in message:
            code = "authentication_required"
        elif "timeout" in message:
            code = "request_timeout"
        elif "connect" in message or "unavailable" in message or "502" in message or "503" in message or "504" in message:
            code = "backend_unavailable"
        else:
            code = "section_failed"
        return name, None, code


async def get_business_summary(
    date: str | None = None, timezone: str | None = None
) -> dict[str, Any]:
    """Summarize backend-supported business indicators for one local calendar day."""
    timezone_name, _ = resolve_timezone(timezone)
    day = date or local_today(timezone_name)
    optional_date(date, "date")
    period = {"date": day, **local_date_period(day, day, timezone_name)}
    results = await asyncio.gather(
        _safe_call("orders", get_orders(limit=100, page=1, start_date=day, end_date=day, timezone=timezone_name)),
        _safe_call("order_stats", get_order_stats()),
        _safe_call("inventory_summary", get_product_summary()),
    )
    sections = {name: value for name, value, error in results if not error}
    failures = [{"section": name, "status": "unavailable", "error_code": error} for name, value, error in results if error]
    order_items = _items(sections.get("orders") or {})
    return {
        "as_of": datetime.now(UTC_ZONE).isoformat(),
        "period": period,
        "orders": _order_summary(order_items),
        "order_stats": (sections.get("order_stats") or {}).get("data", sections.get("order_stats")) if "order_stats" in sections else None,
        "inventory_summary": (sections.get("inventory_summary") or {}).get("data", sections.get("inventory_summary")) if "inventory_summary" in sections else None,
        "unavailable": failures,
        "data_notes": [
            "Order value is order value, not profit or collected cash.",
            "Shipment, return, and detailed work-queue indicators require separate queries.",
        ],
    }


async def get_attention_queue(
    date: str | None = None, timezone: str | None = None
) -> dict[str, Any]:
    """Return evidence-backed attention categories for one local calendar day."""
    timezone_name, _ = resolve_timezone(timezone)
    day = date or local_today(timezone_name)
    optional_date(date, "date")
    period = {"date": day, **local_date_period(day, day, timezone_name)}
    results = await asyncio.gather(
        _safe_call("orders", get_orders(limit=100, page=1, start_date=day, end_date=day, timezone=timezone_name)),
        _safe_call("fulfillment", get_fulfillment_queue(limit=100, page=1)),
        _safe_call("shipments", _shipment_follow_up()),
        _safe_call("returns", search_returns(limit=100, page=1)),
    )
    sections = {name: value for name, value, error in results if not error}
    failures = [{"section": name, "status": "unavailable", "error_code": error} for name, value, error in results if error]
    order_items = [o for o in _items(sections.get("orders") or {}) if o.get("orderStatus") in {"PENDING", "ON_HOLD", "PARTIALLY_FULFILLED"}]
    items: list[dict[str, Any]] = []
    orders_result = sections.get("orders") or {}
    orders_total = _pagination_total(orders_result, len(order_items))
    if orders_total:
        items.append({
            "category": "pending_orders",
            "reason": "Orders returned by the backend for the requested day.",
            "evidence": _evidence(order_items, orders_total, "orderNo"),
        })
    queue_orders = (sections.get("fulfillment") or {}).get("orders", [])
    fulfillment_result = sections.get("fulfillment") or {}
    fulfillment_total = _pagination_total(fulfillment_result, len(queue_orders))
    if fulfillment_total:
        items.append({
            "category": "pending_fulfillment",
            "reason": "Orders are in the backend PENDING fulfillment queue.",
            "evidence": _evidence(queue_orders, fulfillment_total, "order_number"),
        })
    shipment_result = sections.get("shipments") or {}
    shipment_items = shipment_result.get("items", [])
    if shipment_items:
        items.append({
            "category": "shipment_follow_up",
            "reason": "Orders have stored shipments that are not marked delivered; pending fulfillment orders are excluded.",
            "evidence": _evidence(
                shipment_items,
                len(shipment_items),
                "order_number",
                sort_key=_shipment_date,
                total_count_is_partial=shipment_result.get(
                    "total_count_is_partial", False
                ),
            ),
        })
    return_section = sections.get("returns") or {}
    return_items = return_section.get("returns", [])
    return_count = (return_section.get("pagination") or {}).get(
        "totalItems", len(return_items)
    )
    if return_count:
        items.append({
            "category": "returns",
            "reason": "Return-related orders were returned by the backend.",
            "evidence": {
                "count": return_count,
                "total_count": return_count,
                "sample_size": min(len(return_items), 10),
                "is_sample": return_count > min(len(return_items), 10),
                "total_count_is_partial": False,
                "returns": [
                    r.get("return_id")
                    for r in sorted(return_items, key=_record_date)[:10]
                ],
            },
        })
    return {
        "as_of": datetime.now(UTC_ZONE).isoformat(),
        "period": period,
        "items": items,
        "found": bool(items),
        "unavailable": [
            "No backend-supported arbitrary priority ranking was applied.",
            "Separate channel issue and inventory-risk queues are not exposed by the current services.",
        ] + failures,
    }
