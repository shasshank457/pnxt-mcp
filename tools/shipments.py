"""Shipment and fulfillment views backed by existing order responses."""

from typing import Any

from services.validation import optional_id, optional_text, page_limit
from tools.orders import SUPPORTED_ORDER_STATUSES, get_orders
from tools.status_options import SHIP_STATUSES

MAX_SHIPMENT_PAGE_SIZE = 100
SHIPMENT_SCAN_MAX_PAGES = 10
FULFILLMENT_QUEUE_STATUSES = {"PENDING", "CONFIRMED", "ON_HOLD", "PARTIALLY_FULFILLED"}


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
