"""Customer-focused views built from the existing PointNXT orders service."""

import time
from typing import Any

from services.customer_matching import (
    normalize_email,
    normalize_phone,
    order_matches_contact,
)
from services.timezone import local_timestamp
from services.validation import optional_id, optional_text, page_limit
from tools.orders import get_orders
from tools.status_options import RETURN_STATUSES

CUSTOMER_HISTORY_MAX_PAGES = 10
CUSTOMER_SCAN_PAGE_SIZE = 100
CUSTOMER_SCAN_MAX_PAGES = 10
CUSTOMER_INDEX_TTL_SECONDS = 300

_customer_index_cache: dict[str, Any] | None = None


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


def _customer_identity_values(customer: dict[str, Any]) -> set[str]:
    values = set()
    if customer.get("id"):
        values.add(f"id:{customer['id']}")
    if customer.get("email"):
        values.add(f"email:{normalize_email(customer['email'])}")
    if customer.get("phone"):
        values.add(f"phone:{normalize_phone(customer['phone'])}")
    if not values and customer.get("name"):
        values.add(f"name:{customer['name'].strip().lower()}")
    return values


def _merge_customer(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    record_ids = existing.setdefault("record_ids", [existing.get("id")])
    incoming_id = incoming.get("id")
    if incoming_id and incoming_id not in record_ids:
        record_ids.append(incoming_id)
    for record_id in incoming.get("record_ids", []):
        if record_id and record_id not in record_ids:
            record_ids.append(record_id)
    for field in ("id", "name", "email", "phone"):
        if not existing.get(field) and incoming.get(field):
            existing[field] = incoming[field]


def _clear_customer_index_cache() -> None:
    """Clear the short-lived contact index, primarily for tests and lifecycle hooks."""
    global _customer_index_cache
    _customer_index_cache = None


def _index_customer(index: dict[str, Any], customer: dict[str, Any]) -> None:
    record_id = customer.get("id")
    if not record_id:
        return
    for field, key in (
        ("email", normalize_email(customer.get("email"))),
        ("phone", normalize_phone(customer.get("phone"))),
    ):
        if not key:
            continue
        bucket = index[f"by_{field}"].setdefault(key, {})
        existing = bucket.get(record_id)
        if existing:
            _merge_customer(existing, customer)
        else:
            bucket[record_id] = {**customer, "record_ids": [record_id]}


async def _get_customer_index() -> dict[str, Any]:
    global _customer_index_cache
    now = time.monotonic()
    if (
        _customer_index_cache
        and _customer_index_cache["expires_at"] > now
    ):
        return _customer_index_cache

    index: dict[str, Any] = {
        "by_email": {},
        "by_phone": {},
        "orders": [],
        "partial": False,
    }
    for page in range(1, CUSTOMER_SCAN_MAX_PAGES + 1):
        response = await get_orders(limit=CUSTOMER_SCAN_PAGE_SIZE, page=page)
        data = response.get("data", {}) or {}
        items = data.get("items", []) or []
        metadata = data.get("metadata", {}) or {}
        for order in items:
            index["orders"].append(order)
            _index_customer(index, _customer_from_order(order))

        total = metadata.get("totalItems", metadata.get("total"))
        scanned = page * CUSTOMER_SCAN_PAGE_SIZE
        if not items or len(items) < CUSTOMER_SCAN_PAGE_SIZE or (
            isinstance(total, int) and scanned >= total
        ):
            break
        if page == CUSTOMER_SCAN_MAX_PAGES:
            index["partial"] = True

    index["expires_at"] = now + CUSTOMER_INDEX_TTL_SECONDS
    _customer_index_cache = index
    return index


def _contact_index_matches(
    index: dict[str, Any], email: str | None, phone: str | None
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if email:
        records.update(index["by_email"].get(normalize_email(email), {}))
    if phone:
        records.update(index["by_phone"].get(normalize_phone(phone), {}))
    candidates: list[dict[str, Any]] = []
    for customer in records.values():
        match = next(
            (
                candidate
                for candidate in candidates
                if _customer_identity_values(customer)
                & _customer_identity_values(candidate)
            ),
            None,
        )
        if match:
            _merge_customer(match, customer)
        else:
            candidates.append({**customer, "record_ids": [customer["id"]]})
    return candidates


async def orders_for_contact(
    email: str | None,
    phone: str | None,
    limit: int,
    page: int,
    **filters: Any,
) -> dict[str, Any]:
    """Resolve a contact through the index, then fetch orders by customer ID."""
    index = await _get_customer_index()
    candidates = _contact_index_matches(index, email, phone)
    record_ids = [
        record_id
        for candidate in candidates
        for record_id in candidate.get("record_ids", [])
        if record_id
    ]
    orders_by_id: dict[str, dict[str, Any]] = {}
    for record_id in record_ids:
        response = await get_orders(
            limit=CUSTOMER_SCAN_PAGE_SIZE,
            page=1,
            customer_id=record_id,
            **filters,
        )
        for order in (response.get("data", {}) or {}).get("items", []) or []:
            orders_by_id[order.get("id") or f"{record_id}:{len(orders_by_id)}"] = order
    if not record_ids:
        for order in index["orders"]:
            if order_matches_contact(order, email, phone):
                orders_by_id[order.get("id") or f"scan:{len(orders_by_id)}"] = order
    orders = list(orders_by_id.values())
    start = (page - 1) * limit
    page_items = orders[start : start + limit]
    result: dict[str, Any] = {
        "items": page_items,
        "metadata": {
            "page": page,
            "limit": limit,
            "totalItems": len(orders),
        },
    }
    if index["partial"]:
        result["results_are_partial"] = True
        result["note"] = (
            "Customer contact scan reached its maximum page limit; results may be incomplete."
        )
    return {"data": result}


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

    contact_lookup = bool(email or phone)
    if contact_lookup:
        index = await _get_customer_index()
        indexed_candidates = _contact_index_matches(index, email, phone)
        response = {"data": {"metadata": {"page": page, "limit": limit}}}
        candidates = indexed_candidates
    else:
        response = await get_orders(
            limit=limit,
            page=page,
            customer_id=customer_id,
            order_no=name,
        )
        items = response.get("data", {}).get("items", []) or []
        candidates = []
        for order in items:
            customer = _customer_from_order(order)
            identities = _customer_identity_values(customer)
            match = next(
                (
                    candidate
                    for candidate in candidates
                    if identities & _customer_identity_values(candidate)
                ),
                None,
            )
            if match:
                _merge_customer(match, customer)
            elif identities:
                customer["record_ids"] = [customer.get("id")] if customer.get("id") else []
                candidates.append(customer)

        if customer_id and candidates:
            index = await _get_customer_index()
            expanded = []
            for candidate in candidates:
                related = _contact_index_matches(
                    index, candidate.get("email"), candidate.get("phone")
                )
                for related_customer in related:
                    _merge_customer(candidate, related_customer)
                expanded.append(candidate)
            candidates = expanded

    results = candidates
    partial = bool(contact_lookup and index["partial"]) if contact_lookup else False
    if customer_id and candidates:
        partial = bool(index["partial"])
    pagination = response.get("data", {}).get("metadata", {})
    if partial:
        pagination = {**pagination, "results_are_partial": True}
    return {
        "found": bool(results),
        "ambiguous": len(results) > 1,
        "candidates": results,
        "pagination": pagination,
        "message": None if results else "No matching customer found.",
        **(
            {
                "results_are_partial": True,
                "note": "Customer contact scan reached its maximum page limit; results may be incomplete.",
            }
            if partial
            else {}
        ),
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
    record_ids = [
        record_id
        for record_id in (customer.get("record_ids") or [customer.get("id")])
        if record_id
    ]
    order_responses = [
        await get_orders(limit=limit, page=page, customer_id=record_id)
        for record_id in record_ids
        if record_id
    ]
    orders = [
        order
        for response in order_responses
        for order in response.get("data", {}).get("items", []) or []
    ]
    metadata = order_responses[0].get("data", {}).get("metadata", {}) if order_responses else {}
    history_orders, total_orders, totals_are_partial = await _customer_history(
        record_ids
    )
    page_value = sum(
        order.get("grandTotal", order.get("orderTotal", 0)) or 0 for order in orders
    )
    total_value = sum(
        order.get("grandTotal", order.get("orderTotal", 0)) or 0
        for order in history_orders
    )
    status_counts: dict[str, int] = {}
    for order in history_orders:
        status = order.get("orderStatus")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
    dates = [
        order.get("orderDate") or order.get("createdAt")
        for order in history_orders
        if order.get("orderDate") or order.get("createdAt")
    ]
    product_returns = sum(
        1 for order in history_orders if order.get("orderStatus") in RETURN_STATUSES
    )
    return {
        "found": True,
        "ambiguous": False,
        "customer": customer,
        "statistics": {
            "orders_in_page": len(orders),
            "order_value_in_page": page_value,
            "total_orders": total_orders,
            "total_order_value": total_value,
            "status_counts": dict(sorted(status_counts.items())),
            "first_order_date": min(dates) if dates else None,
            "last_order_date": max(dates) if dates else None,
            "totals_are_partial": totals_are_partial,
            "product_returns": product_returns,
        },
        "orders": [
            {
                "id": order.get("id"),
                "order_number": order.get("orderNo"),
                "channel_order_number": order.get("channelOrderNo"),
                "date": order.get("orderDate"),
                "order_date": order.get("orderDate"),
                "order_date_local": local_timestamp(order.get("orderDate")),
                "status": order.get("orderStatus"),
                "total": order.get("grandTotal", order.get("orderTotal")),
                "items": [
                    {"name": item.get("name"), "quantity": item.get("quantityOrdered")}
                    for item in order.get("orderItems", [])
                ],
            }
            for order in orders
        ],
        "pagination": metadata,
    }


async def _customer_history(
    record_ids: list[str],
) -> tuple[list[dict[str, Any]], int, bool]:
    history: dict[str, dict[str, Any]] = {}
    total_orders = 0
    totals_are_partial = False
    for record_id in record_ids:
        page = 1
        record_total: int | None = None
        record_items = 0
        while page <= CUSTOMER_HISTORY_MAX_PAGES:
            response = await get_orders(
                limit=100,
                page=page,
                customer_id=record_id,
            )
            data = response.get("data", {})
            items = data.get("items", []) or []
            metadata = data.get("metadata", {}) or {}
            total = metadata.get("totalItems", metadata.get("total"))
            if isinstance(total, int):
                record_total = total
            for order in items:
                order_id = order.get("id") or f"{record_id}:{record_items}"
                history[order_id] = order
                record_items += 1
            if not items or (
                isinstance(record_total, int) and record_items >= record_total
            ) or (not isinstance(record_total, int) and len(items) < 100):
                break
            if page == CUSTOMER_HISTORY_MAX_PAGES:
                totals_are_partial = True
                break
            page += 1
        if record_total is None:
            record_total = record_items
        total_orders += record_total
        if page > CUSTOMER_HISTORY_MAX_PAGES:
            totals_are_partial = True
    return list(history.values()), total_orders, totals_are_partial
