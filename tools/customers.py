"""Customer-focused views built from the existing PointNXT orders service."""

from typing import Any

from services.validation import optional_id, optional_text, page_limit
from tools.orders import get_orders


def _customer_from_order(order: dict[str, Any]) -> dict[str, Any]:
    customer = order.get("customer") or {}
    address = order.get("billingAddress") or order.get("shippingAddressSnapshot") or {}
    first = customer.get("firstName") or address.get("firstName")
    last = customer.get("lastName") or address.get("lastName")
    return {
        "id": customer.get("id") or order.get("customerId"),
        "name": customer.get("name")
        or " ".join(part for part in (first, last) if part),
        "email": customer.get("email") or address.get("email"),
        "phone": customer.get("phone") or address.get("phone"),
    }


def _candidate_key(customer: dict[str, Any]) -> str:
    return (
        customer.get("id")
        or customer.get("email")
        or customer.get("phone")
        or customer.get("name", "").strip().lower()
    )


async def find_customers(
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    customer_id: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Find customer candidates using customer fields supported by orders."""
    if not any((name, email, phone, customer_id)):
        raise ValueError("Provide name, email, phone, or customer_id")
    page_limit(limit, page)
    optional_text(name, "name")
    optional_text(email, "email")
    optional_text(phone, "phone")
    optional_id(customer_id, "customer_id")

    response = await get_orders(
        limit=limit,
        page=page,
        customer_id=customer_id,
        customer_email=email,
        customer_phone=phone,
        order_no=name,
    )
    items = response.get("data", {}).get("items", []) or []
    candidates: dict[str, dict[str, Any]] = {}
    for order in items:
        customer = _customer_from_order(order)
        key = _candidate_key(customer)
        if key:
            candidates[key] = customer

    results = list(candidates.values())
    return {
        "found": bool(results),
        "ambiguous": len(results) > 1,
        "candidates": results,
        "pagination": response.get("data", {}).get("metadata", {}),
        "message": None if results else "No matching customer found.",
    }


async def get_customer_context(
    customer_id: str | None = None,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Return compact customer details, order history, and derived statistics."""
    if customer_id:
        candidates = await find_customers(customer_id=customer_id, limit=limit, page=page)
    else:
        candidates = await find_customers(
            name=name, email=email, phone=phone, limit=limit, page=page
        )
    matches = candidates["candidates"]
    if not matches:
        return {"found": False, "customer": None, "message": "No matching customer found."}
    if len(matches) > 1:
        return {
            "found": True,
            "ambiguous": True,
            "candidates": matches,
            "message": "Multiple customers matched; provide a customer_id or more specific contact information.",
        }

    customer = matches[0]
    orders_response = await get_orders(
        limit=limit,
        page=page,
        customer_id=customer.get("id"),
        customer_email=customer.get("email") if not customer.get("id") else None,
        customer_phone=customer.get("phone") if not customer.get("id") else None,
    )
    orders = orders_response.get("data", {}).get("items", []) or []
    total_value = sum(
        order.get("grandTotal", order.get("orderTotal", 0)) or 0 for order in orders
    )
    return {
        "found": True,
        "ambiguous": False,
        "customer": customer,
        "statistics": {
            "orders_returned": len(orders),
            "total_orders": orders_response.get("data", {}).get("metadata", {}).get(
                "totalItems", len(orders)
            ),
            "order_value_returned": total_value,
            "status_counts": {
                status: sum(1 for order in orders if order.get("orderStatus") == status)
                for status in sorted({order.get("orderStatus") for order in orders if order.get("orderStatus")})
            },
        },
        "orders": [
            {
                "id": order.get("id"),
                "order_number": order.get("orderNo"),
                "channel_order_number": order.get("channelOrderNo"),
                "date": order.get("orderDate"),
                "status": order.get("orderStatus"),
                "total": order.get("grandTotal", order.get("orderTotal")),
                "items": [
                    {"name": item.get("name"), "quantity": item.get("quantityOrdered")}
                    for item in order.get("orderItems", [])
                ],
            }
            for order in orders
        ],
        "pagination": orders_response.get("data", {}).get("metadata", {}),
    }
