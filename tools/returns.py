"""Return views backed by the existing PointNXT order service."""

from typing import Any

from services.validation import optional_id, optional_text, page_limit
from services.tool_errors import unsupported_operation
from tools.orders import get_orders
from tools.status_options import RETURN_STATUSES


async def _all_orders_for_status(
    status: str,
    order_reference: str | None,
    customer_id: str | None,
    warehouse_id: str | None,
) -> list[dict[str, Any]]:
    """Fetch every page for one return status before merging statuses."""
    orders: list[dict[str, Any]] = []
    backend_limit = 100
    backend_page = 1
    while True:
        response = await get_orders(
            limit=backend_limit,
            page=backend_page,
            order_no=order_reference,
            order_status=status,
            customer_id=customer_id,
            warehouse_id=warehouse_id,
        )
        data = response.get("data", {})
        items = data.get("items", []) or []
        orders.extend(items)
        metadata = data.get("metadata", {}) or {}
        total = metadata.get("totalItems", metadata.get("total"))
        if not items or len(items) < backend_limit or (
            isinstance(total, int) and len(orders) >= total
        ):
            return orders
        backend_page += 1


def _order_date(order: dict[str, Any]) -> str:
    return order.get("orderDate") or order.get("createdAt") or ""


def _return_item(item: dict[str, Any]) -> dict[str, Any]:
    """Expose only order-backed item fields; return quantities are not confirmed."""
    return {
        key: value
        for key, value in {
            "sku": item.get("sku") or item.get("code") or item.get("channelSku"),
            "name": item.get("name"),
            "quantity_ordered": item.get("quantityOrdered", item.get("quantity")),
        }.items()
        if value is not None
    }


def _return_shipment(shipment: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "awb_no": shipment.get("awbNo") or shipment.get("awb"),
            "ship_status": shipment.get("shipStatus"),
            "created_at": shipment.get("createdAt"),
            "delivered_at": shipment.get("deliveredAt"),
            "is_return": True,
        }.items()
        if value is not None or key == "delivered_at"
    }


def _return_view(order: dict[str, Any]) -> dict[str, Any]:
    customer = order.get("customer") or {}
    view = {
        "data_source": "order_status",
        "note": (
            "Return data is derived from order status. Item-level return quantities, "
            "reasons and inspection details are not available from the backend."
        ),
        "order_id": order.get("id"),
        "order_number": order.get("orderNo"),
        "channel_order_number": order.get("channelOrderNo"),
        "customer": customer,
        "status": order.get("returnStatus") or order.get("orderStatus"),
        "return_reason": order.get("returnReason"),
        "requested_at": order.get("returnRequestedAt"),
        "received_at": order.get("returnReceivedAt"),
        "items": [_return_item(item) for item in order.get("orderItems", [])],
        "refund_status": order.get("refundStatus"),
        "refund_amount": order.get("refundAmount"),
        "refund_execution_available": False,
    }
    return_shipments = [
        _return_shipment(shipment)
        for shipment in order.get("shipments", []) or []
        if shipment.get("isReturn") is True
    ]
    if return_shipments:
        view["return_shipments"] = return_shipments
    return {key: value for key, value in view.items() if value is not None}


async def search_returns(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    return_status: str | None = None,
    customer_id: str | None = None,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """Search orders by six return-related statuses; data is derived from order status."""
    page_limit(limit, page)
    optional_text(order_reference, "order_reference")
    optional_id(customer_id, "customer_id")
    optional_id(warehouse_id, "warehouse_id")
    if return_status is not None:
        optional_text(return_status, "return_status")
        normalized = return_status.upper()
        if normalized not in RETURN_STATUSES:
            raise ValueError(
                f"Invalid return_status {return_status!r}. Valid values: "
                f"{', '.join(sorted(RETURN_STATUSES))}"
            )
    if return_status is not None:
        response = await get_orders(
            limit=limit,
            page=page,
            order_no=order_reference,
            order_status=return_status.upper(),
            customer_id=customer_id,
            warehouse_id=warehouse_id,
        )
        data = response.get("data", {})
        items = data.get("items", []) or []
        pagination = data.get("metadata", {})
    else:
        items = []
        for status in RETURN_STATUSES:
            items.extend(
                await _all_orders_for_status(
                    status, order_reference, customer_id, warehouse_id
                )
            )
        items.sort(key=_order_date, reverse=True)
        total_items = len(items)
        start = (page - 1) * limit
        items = items[start : start + limit]
        pagination = {
            "page": page,
            "limit": limit,
            "totalItems": total_items,
            "in_page": len(items),
        }
    return {
        "found": bool(items),
        "returns": [_return_view(order) for order in items],
        "pagination": pagination,
        "message": None if items else "No matching return records found.",
    }


async def get_order_returns(order_reference: str) -> dict[str, Any]:
    """Show order-status-derived return information for a display or channel order reference."""
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


async def get_return_details(
    order_reference: str | None = None,
    return_request_id: str | None = None,
) -> dict[str, Any]:
    """Return the backend-supported return view for an order/reference.

    PointNXT currently exposes returns only through order records.  A dedicated
    return_request_id therefore cannot be resolved and is rejected explicitly.
    """
    optional_text(order_reference, "order_reference")
    optional_text(return_request_id, "return_request_id")
    if return_request_id:
        raise ValueError(
            "The PointNXT backend has no dedicated return-request endpoint or return IDs; provide order_reference."
        )
    if not order_reference:
        raise ValueError("order_reference is required because returns are derived from orders")
    return await get_order_returns(order_reference)


async def find_pending_returns(
    limit: int = 20,
    page: int = 1,
    customer_id: str | None = None,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """List orders in the actual backend status used for pending return action."""
    return await search_returns(
        limit=limit,
        page=page,
        return_status="RETURN_REQUESTED",
        customer_id=customer_id,
        warehouse_id=warehouse_id,
    )


async def confirm_return_request(
    order_reference: str,
    reason: str | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Validate an order return candidate, then fail closed without a write endpoint."""
    optional_text(order_reference, "order_reference", required=True)
    optional_text(reason, "reason")
    if confirmation_token is not None or idempotency_key is not None:
        optional_text(confirmation_token, "confirmation_token")
        optional_text(idempotency_key, "idempotency_key")
    details = await get_order_returns(order_reference)
    if not details["found"]:
        raise ValueError("Order is not currently represented in a backend return status")
    result = {
        "status": "preview",
        "action": "confirm_return_request",
        "order_reference": order_reference.strip().lstrip("#"),
        "current_return_records": details["returns"],
        "reason": reason,
        "confirmation_required": True,
        "backend_capability": False,
        "message": "Preview only. Explicit confirmation is required, but PointNXT currently has no endpoint to create a return request.",
    }
    if confirm:
        raise unsupported_operation(
            "a return-request create/confirm endpoint",
            action="confirm_return_request",
            details={"order_reference": result["order_reference"], "confirmation_required": True},
        )
    return {key: value for key, value in result.items() if value is not None}


async def create_support_ticket(
    subject: str,
    description: str,
    order_reference: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Report that ticket creation is unavailable; never store a fake ticket."""
    optional_text(subject, "subject", required=True)
    optional_text(description, "description", required=True)
    optional_text(order_reference, "order_reference")
    optional_text(idempotency_key, "idempotency_key")
    raise unsupported_operation(
        "a support-ticket creation endpoint or ticketing integration",
        action="create_support_ticket",
    )
