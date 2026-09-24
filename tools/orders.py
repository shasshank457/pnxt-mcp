import re
from datetime import datetime, timezone
from typing import Any

from services.pointnxt_api import PointNXTAPI


def get_api() -> PointNXTAPI:
    return PointNXTAPI()


SUPPORTED_ORDER_STATUSES = {
    "PENDING",
    "CONFIRMED",
    "ON_HOLD",
    "PARTIALLY_FULFILLED",
    "FULFILLED",
    "DELIVERED",
    "CANCELLED",
    "RETURN_REQUESTED",
    "RETURN_RECEIVED",
    "PARTIALLY_RETURNED",
    "RETURNED",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
    "DISPUTED",
    "CLOSED",
}


def _validate_order_filters(
    limit: int,
    page: int,
    order_status: str | None,
    start_date: str | None,
    end_date: str | None,
) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise ValueError("page must be a positive integer")

    if order_status is not None:
        if not isinstance(order_status, str):
            raise ValueError("order_status must be a string")
        normalized_status = order_status.upper()
        if normalized_status not in SUPPORTED_ORDER_STATUSES:
            raise ValueError(
                f"order_status must be one of: "
                f"{', '.join(sorted(SUPPORTED_ORDER_STATUSES))}"
            )

    date_pattern = r"^\d{4}-\d{2}-\d{2}$"
    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(date_pattern, value):
                raise ValueError(f"{name} must use YYYY-MM-DD format")
            try:
                datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError as error:
                raise ValueError(f"{name} must use YYYY-MM-DD format") from error

    if start_date and end_date and end_date < start_date:
        raise ValueError("end_date cannot be before start_date")


async def get_orders(
    limit: int = 10,
    page: int = 1,
    order_status: str | None = None,
    payment_method: str | None = None,
    channel_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    order_no: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    channel_order_id: str | None = None,
) -> dict[str, Any]:
    _validate_order_filters(limit, page, order_status, start_date, end_date)

    params = {}
    optional_params = {
        "limit": limit,
        "page": page,
        "orderStatus": order_status.upper() if order_status else None,
        "paymentMethod": payment_method,
        "channelId": channel_id,
        "sellerId": seller_id,
        "warehouseId": warehouse_id,
        "customerId": customer_id,
        "dateFrom": start_date,
        "dateTo": end_date,
        "search": (order_no or customer_email or customer_phone or channel_order_id),
    }

    for name, value in optional_params.items():
        if value is not None:
            params[name] = value

    return await get_api().async_get("/commerce/orders", params=params)


async def get_orders_summary(
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    orders_response = await get_orders(
        limit=limit if limit is not None else 10,
        page=page if page is not None else 1,
        order_status=order_status,
        start_date=start_date,
        end_date=end_date,
    )
    data = orders_response.get("data", {})
    orders = data.get("items", []) or []

    def order_amount(order: dict[str, Any]) -> float:
        return order.get("grandTotal", order.get("orderTotal", 0)) or 0

    total_order_value = sum(order_amount(order) for order in orders)
    status_breakdown = {}
    payment_method_breakdown = {}

    for order in orders:
        status = order.get("orderStatus") or "UNKNOWN"
        payment_method = order.get("paymentMethod") or "UNKNOWN"
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        payment_method_breakdown[payment_method] = (
            payment_method_breakdown.get(payment_method, 0) + 1
        )

    recent_orders = sorted(
        orders,
        key=lambda order: order.get("orderDate") or order.get("createdAt") or "",
        reverse=True,
    )[:5]

    return {
        "total_orders": len(orders),
        "total_order_value": total_order_value,
        "average_order_value": (total_order_value / len(orders) if orders else 0),
        "status_breakdown": status_breakdown,
        "payment_method_breakdown": payment_method_breakdown,
        "top_5_recent_orders": [
            {
                "order_number": order.get("orderNo"),
                "customer": order.get("customer"),
                "amount": order_amount(order),
                "status": order.get("orderStatus"),
            }
            for order in recent_orders
        ],
    }


async def get_order_by_id(order_id: str) -> dict[str, Any]:
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be a non-empty string")

    return await get_api().async_get(f"/commerce/orders/{order_id}")


async def get_order_stats() -> dict[str, Any]:
    return await get_api().async_get("/commerce/orders/stats")


async def get_status_transitions() -> dict[str, Any]:
    return await get_api().async_get("/commerce/orders/status-transitions")
