"""Read-only business-assistant summaries composed from existing tools."""

import asyncio
from datetime import datetime, timezone
from typing import Any

from services.validation import optional_date
from tools.orders import get_order_stats, get_orders
from tools.products import get_product_summary
from tools.returns import search_returns
from tools.shipments import get_fulfillment_queue, search_shipments


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


async def get_business_summary(date: str | None = None) -> dict[str, Any]:
    """Summarize backend-supported business indicators for one UTC calendar day."""
    day = date or datetime.now(timezone.utc).date().isoformat()
    optional_date(date, "date")
    results = await asyncio.gather(
        _safe_call("orders", get_orders(limit=100, page=1, start_date=day, end_date=day)),
        _safe_call("order_stats", get_order_stats()),
        _safe_call("inventory_summary", get_product_summary()),
    )
    sections = {name: value for name, value, error in results if not error}
    failures = [{"section": name, "status": "unavailable", "error_code": error} for name, value, error in results if error]
    order_items = _items(sections.get("orders") or {})
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "period": {"date": day, "timezone": "UTC"},
        "orders": _order_summary(order_items),
        "order_stats": (sections.get("order_stats") or {}).get("data", sections.get("order_stats")) if "order_stats" in sections else None,
        "inventory_summary": (sections.get("inventory_summary") or {}).get("data", sections.get("inventory_summary")) if "inventory_summary" in sections else None,
        "unavailable": failures,
        "data_notes": [
            "Order value is order value, not profit or collected cash.",
            "Shipment, return, and detailed work-queue indicators require separate queries.",
        ],
    }


async def get_attention_queue(date: str | None = None) -> dict[str, Any]:
    """Return evidence-backed attention categories from existing backend data."""
    day = date or datetime.now(timezone.utc).date().isoformat()
    optional_date(date, "date")
    results = await asyncio.gather(
        _safe_call("orders", get_orders(limit=100, page=1, start_date=day, end_date=day)),
        _safe_call("fulfillment", get_fulfillment_queue(limit=100, page=1)),
        _safe_call("shipments", search_shipments(limit=100, page=1, order_status="PENDING")),
        _safe_call("returns", search_returns(limit=100, page=1)),
    )
    sections = {name: value for name, value, error in results if not error}
    failures = [{"section": name, "status": "unavailable", "error_code": error} for name, value, error in results if error]
    order_items = [o for o in _items(sections.get("orders") or {}) if o.get("orderStatus") in {"PENDING", "ON_HOLD", "PARTIALLY_FULFILLED"}]
    items: list[dict[str, Any]] = []
    if order_items:
        items.append({
            "category": "pending_orders",
            "reason": "Orders returned by the backend for the requested day.",
            "evidence": {"count": len(order_items), "orders": [o.get("orderNo") for o in order_items]},
        })
    queue_orders = (sections.get("fulfillment") or {}).get("orders", [])
    if queue_orders:
        items.append({
            "category": "pending_fulfillment",
            "reason": "Orders are in the backend PENDING fulfillment queue.",
            "evidence": {"count": len(queue_orders), "orders": [o.get("order_number") for o in queue_orders]},
        })
    shipment_items = (sections.get("shipments") or {}).get("shipments", [])
    if shipment_items:
        items.append({
            "category": "shipment_follow_up",
            "reason": "Pending orders were returned by the shipment view; stored tracking was not refreshed.",
            "evidence": {"count": len(shipment_items), "orders": [s.get("order_number") for s in shipment_items]},
        })
    return_items = (sections.get("returns") or {}).get("returns", [])
    if return_items:
        items.append({
            "category": "returns",
            "reason": "Return-related orders were returned by the backend.",
            "evidence": {"count": len(return_items), "returns": [r.get("return_id") for r in return_items]},
        })
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "period": {"date": day, "timezone": "UTC"},
        "items": items,
        "found": bool(items),
        "unavailable": [
            "No backend-supported arbitrary priority ranking was applied.",
            "Separate channel issue and inventory-risk queues are not exposed by the current services.",
        ] + failures,
    }
