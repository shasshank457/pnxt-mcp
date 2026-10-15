from typing import Any

from services.customer_matching import (
    normalize_email,
    normalize_phone,
    order_matches_contact,
)
from services.pointnxt_api import PointNXTAPI
from services.timezone import local_date_period, local_timestamp, resolve_timezone
from tools.status_options import ORDER_STATUSES


def get_api() -> PointNXTAPI:
    return PointNXTAPI()


SUPPORTED_ORDER_STATUSES = frozenset(ORDER_STATUSES)
MAX_ORDER_PAGE_SIZE = 100


def _validate_order_filters(
    limit: int,
    page: int,
    order_status: str | None,
    start_date: str | None,
    end_date: str | None,
    timezone_name: str | None,
) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if limit > MAX_ORDER_PAGE_SIZE:
        raise ValueError(f"limit must be at most {MAX_ORDER_PAGE_SIZE}")
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

    local_date_period(start_date, end_date, timezone_name)


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
    timezone: str | None = None,
) -> dict[str, Any]:
    _validate_order_filters(limit, page, order_status, start_date, end_date, timezone)
    period = local_date_period(start_date, end_date, timezone)
    timezone_name, _ = resolve_timezone(timezone)

    params = {}
    search = order_no
    if not search and customer_email:
        search = normalize_email(customer_email)
    elif not search and customer_phone:
        search = normalize_phone(customer_phone)
    optional_params = {
        "limit": limit,
        "page": page,
        "orderStatus": order_status.upper() if order_status else None,
        "paymentMethod": payment_method,
        "channelId": channel_id,
        "sellerId": seller_id,
        "warehouseId": warehouse_id,
        "customerId": customer_id,
        "dateFrom": period["utc_start"],
        "dateTo": period["utc_end"],
        "search": (search or channel_order_id),
    }

    for name, value in optional_params.items():
        if value is not None:
            params[name] = value

    response = await get_api().async_get("/commerce/orders", params=params)
    return {**response, "timezone": timezone_name, "period": period}


async def get_orders_summary(
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    orders_response = await get_orders(
        limit=limit if limit is not None else 10,
        page=page if page is not None else 1,
        order_status=order_status,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
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
        "found": bool(orders),
        "period": {
            "start_date": start_date,
            "end_date": end_date,
            **local_date_period(start_date, end_date, timezone),
        },
        "timezone": resolve_timezone(timezone)[0],
        "pagination": {
            "page": page if page is not None else 1,
            "limit": limit if limit is not None else 10,
            "in_page": len(orders),
            "total": data.get("totalItems", data.get("total", len(orders))),
        },
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


async def search_orders(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    payment_method: str | None = None,
    channel_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    channel_order_id: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Search orders using the existing PointNXT order-list endpoint.

    PointNXT exposes one combined order search parameter, so customer_name is
    passed through the same backend search field as the other text searches.
    """
    result = await get_orders(
        limit=limit,
        page=page,
        order_status=order_status,
        payment_method=payment_method,
        channel_id=channel_id,
        seller_id=seller_id,
        warehouse_id=warehouse_id,
        customer_id=customer_id,
        start_date=start_date,
        end_date=end_date,
        order_no=order_reference or customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        channel_order_id=channel_order_id,
        timezone=timezone,
    )
    data = result.get("data", {})
    items = data.get("items", []) or []
    if customer_email or customer_phone:
        items = [
            order
            for order in items
            if order_matches_contact(order, customer_email, customer_phone)
        ]
    return {
        "found": bool(items),
        "timezone": resolve_timezone(timezone)[0],
        "period": local_date_period(start_date, end_date, timezone),
        "pagination": data.get("metadata", {}),
        "orders": [
            {
                "id": order.get("id"),
                "order_number": order.get("orderNo"),
                "channel_order_number": order.get("channelOrderNo"),
                "order_date": order.get("orderDate"),
                "order_date_local": local_timestamp(order.get("orderDate"), timezone),
                "status": order.get("orderStatus"),
                "customer": order.get("customer"),
                "total": order.get("grandTotal", order.get("orderTotal")),
                "currency": order.get("currency"),
            }
            for order in items
        ],
    }


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _non_zero(value: Any) -> bool:
    return value is not None and value != 0


def _compact_order(order: dict[str, Any], timezone_name: str | None = None) -> dict[str, Any]:
    """Shape one order into the support-focused response used by ``get_order``."""
    customer = order.get("customer") or {}
    address = order.get("shippingAddress") or order.get("billingAddress") or {}
    first_name = customer.get("firstName") or address.get("firstName")
    last_name = customer.get("lastName") or address.get("lastName")
    customer_name = customer.get("name") or " ".join(
        part for part in (first_name, last_name) if part
    )

    compact_items = []
    for item in order.get("orderItems", []) or []:
        compact_items.append(
            _without_none({
                "name": item.get("name"),
                "sku": item.get("sku") or item.get("code") or item.get("channelSku"),
                "quantity_ordered": item.get("quantityOrdered"),
                "quantity_shipped": item.get("quantityShipped"),
                "quantity_returned": item.get("quantityReturned"),
                "item_status": item.get("itemStatus") or item.get("status"),
                "unit_price": item.get("unitPrice") or item.get("price"),
                "line_total": item.get("lineTotal") or item.get("totalPrice"),
                **{
                    key: value
                    for key, value in {
                        "tax": item.get("taxAmount", item.get("tax")),
                        "discount": item.get("discountAmount", item.get("discount")),
                        "shipping": item.get("shippingAmount", item.get("shipping")),
                        "cod_fee": item.get("codFee", item.get("codAmount")),
                        "gift_wrap": item.get("giftWrapAmount", item.get("giftWrapFee")),
                    }.items()
                    if _non_zero(value)
                },
            })
        )

    compact_shipments = []
    for shipment in order.get("shipments", []) or []:
        shipment_values = {
            "awb_no": shipment.get("awbNo") or shipment.get("awb"),
            "ship_status": shipment.get("shipStatus"),
            "piece_count": shipment.get("pieceCount"),
            "weight_grams": shipment.get("weightGrams"),
            "cod_collectable_amount": shipment.get("codCollectableAmount"),
            "is_return": shipment.get("isReturn"),
            "created_at": shipment.get("createdAt"),
            # A null delivered_at is meaningful: it means not delivered yet.
            "delivered_at": shipment.get("deliveredAt"),
        }
        compact_shipments.append(
            {
                key: value
                for key, value in shipment_values.items()
                if value is not None or key == "delivered_at"
            }
        )

    warehouse = order.get("warehouse") or {}
    compact = {
        "id": order.get("id"),
        "order_number": order.get("orderNo"),
        "channel_order_number": order.get("channelOrderNo"),
        "order_date": order.get("orderDate"),
        "order_date_local": local_timestamp(order.get("orderDate"), timezone_name),
        "status": order.get("orderStatus"),
        "customer": _without_none({
            "id": customer.get("id") or order.get("customerId"),
            "name": customer_name or None,
            "email": customer.get("email") or address.get("email"),
            "phone": customer.get("phone") or address.get("phone"),
            "state": customer.get("state") or address.get("state"),
        }),
        "items": compact_items,
        "payment": _without_none({
            "method": order.get("paymentMethod"),
            "total": order.get("grandTotal", order.get("orderTotal")),
            "paid": order.get("paidAmount"),
            "refunded": order.get("refundAmount"),
            "currency": order.get("currency"),
        }),
        "fulfillment": _without_none({
            "warehouse": _without_none({
                "id": warehouse.get("id"),
                "code": warehouse.get("code"),
                "name": warehouse.get("name"),
            }),
            "address_validation_status": order.get("addressValidationStatus"),
        }),
        "shipments": compact_shipments,
        "returns": _without_none({
            "returned_at": order.get("returnedAt"),
            "return_reason": order.get("returnReason"),
        }),
    }
    return _without_none(compact)


async def get_order(order_reference: str) -> dict[str, Any]:
    """Retrieve compact support-focused details by order ID or display/reference number."""
    if not isinstance(order_reference, str) or not order_reference.strip():
        raise ValueError("order_reference must be a non-empty string")

    reference = order_reference.strip()
    reference = reference.removeprefix("#")
    search = await get_orders(limit=1, page=1, order_no=reference)
    items = search.get("data", {}).get("items", []) or []
    if not items:
        return {"found": False, "order": None, "message": "No matching order found."}
    detail = await get_order_by_id(items[0]["id"])
    order = detail.get("data", detail)
    return {"found": True, "order": _compact_order(order)}


async def get_order_by_id(order_id: str) -> dict[str, Any]:
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be a non-empty string")

    return await get_api().async_get(f"/commerce/orders/{order_id}")


async def get_order_stats() -> dict[str, Any]:
    return await get_api().async_get("/commerce/orders/stats")


async def get_status_transitions() -> dict[str, Any]:
    return await get_api().async_get("/commerce/orders/status-transitions")
