"""Return views backed by the existing PointNXT order service."""

from typing import Any

from tools.orders import get_orders
from services.validation import optional_id, optional_text, page_limit


RETURN_STATUSES = {
    "RETURN_REQUESTED",
    "RETURN_RECEIVED",
    "PARTIALLY_RETURNED",
    "RETURNED",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
}


def _return_item(item: dict[str, Any]) -> dict[str, Any]:
    """Expose backend-provided return quantities without deriving them."""
    return {
        "id": item.get("id"),
        "sku": item.get("sku") or item.get("code") or item.get("channelSku"),
        "name": item.get("name"),
        "requested_quantity": item.get("returnQuantity", item.get("quantityReturned")),
        "accepted_quantity": item.get("acceptedQuantity"),
        "rejected_quantity": item.get("rejectedQuantity"),
        "inspection_status": item.get("inspectionStatus"),
        "putaway_status": item.get("putawayStatus"),
        "stock_restoration_status": item.get("stockRestorationStatus"),
        "raw_return_fields_available": any(
            key in item
            for key in (
                "returnQuantity", "quantityReturned", "acceptedQuantity",
                "rejectedQuantity", "inspectionStatus", "putawayStatus",
                "stockRestorationStatus",
            )
        ),
    }


def _return_view(order: dict[str, Any]) -> dict[str, Any]:
    customer = order.get("customer") or {}
    return {
        "return_id": order.get("returnId") or order.get("id"),
        "order_id": order.get("id"),
        "order_number": order.get("orderNo"),
        "channel_order_number": order.get("channelOrderNo"),
        "customer": customer,
        "status": order.get("returnStatus") or order.get("orderStatus"),
        "return_reason": order.get("returnReason"),
        "requested_at": order.get("returnRequestedAt"),
        "received_at": order.get("returnedAt") or order.get("returnReceivedAt"),
        "inspection_status": order.get("inspectionStatus"),
        "putaway_status": order.get("putawayStatus"),
        "stock_restoration_status": order.get("stockRestorationStatus"),
        "items": [_return_item(item) for item in order.get("orderItems", [])],
        "refund_status": order.get("refundStatus"),
        "refund_amount": order.get("refundAmount"),
        "refund_execution_available": False,
    }


async def search_returns(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    return_status: str | None = None,
    customer_id: str | None = None,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """Search return-related orders using existing order status filters."""
    page_limit(limit, page)
    optional_text(order_reference, "order_reference")
    optional_id(customer_id, "customer_id")
    optional_id(warehouse_id, "warehouse_id")
    if return_status is not None:
        optional_text(return_status, "return_status")
        normalized = return_status.upper()
        if normalized not in RETURN_STATUSES:
            raise ValueError(f"return_status must be one of: {', '.join(sorted(RETURN_STATUSES))}")
    response = await get_orders(
        limit=limit,
        page=page,
        order_no=order_reference,
        order_status=return_status.upper() if return_status else "RETURN_REQUESTED",
        customer_id=customer_id,
        warehouse_id=warehouse_id,
    )
    data = response.get("data", {})
    items = data.get("items", []) or []
    return {
        "found": bool(items),
        "returns": [_return_view(order) for order in items],
        "pagination": data.get("metadata", {}),
        "message": None if items else "No matching return records found.",
    }


async def get_order_returns(order_reference: str) -> dict[str, Any]:
    """Find return information for a display or channel order reference."""
    optional_text(order_reference, "order_reference", required=True)
    reference = order_reference.strip().lstrip("#")
    matches = []
    for status in RETURN_STATUSES:
        response = await get_orders(limit=100, page=1, order_no=reference, order_status=status)
        matches.extend(response.get("data", {}).get("items", []) or [])
    unique = {order.get("id"): order for order in matches if order.get("id")}
    returns = [_return_view(order) for order in unique.values()]
    return {
        "found": bool(returns),
        "returns": returns,
        "message": None if returns else "No return records found for this order.",
    }
