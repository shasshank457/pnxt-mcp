"""Tests for normalized customer contact search and grouping."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.customer_matching import normalize_email, normalize_phone
from tools.customers import find_customers, get_customer_context
from tools.orders import get_orders, search_orders


def _order(customer, order_id="order-1", total=100):
    return {
        "id": order_id,
        "orderNo": order_id,
        "grandTotal": total,
        "customer": customer,
        "orderItems": [],
    }


def test_contact_normalization():
    assert {normalize_phone(value) for value in (
        "+919380027066",
        "919380027066",
        "09380027066",
        "9380027066",
    )} == {"9380027066"}
    assert normalize_email(" VILAS@adaptnxt.com ") == "vilas@adaptnxt.com"


@pytest.mark.asyncio
async def test_contact_records_group_by_email_and_phone():
    response = {
        "data": {
            "items": [
                _order(
                    {
                        "id": "record-1",
                        "name": "Vilas Katigar",
                        "email": "vilas@adaptnxt.com",
                        "phone": "+919380027066",
                    }
                ),
                _order(
                    {
                        "id": "record-2",
                        "name": "Vilas Katigar",
                        "email": "VILAS@ADAPTNXT.COM",
                        "phone": "9380027066",
                    },
                    order_id="order-2",
                ),
            ]
        }
    }
    with patch("tools.customers.get_orders", new=AsyncMock(return_value=response)):
        result = await find_customers(phone="+919380027066")
    assert result["found"] is True
    assert result["ambiguous"] is False
    assert result["candidates"][0]["record_ids"] == ["record-1", "record-2"]


@pytest.mark.asyncio
async def test_name_search_keeps_existing_backend_search_request():
    api = MagicMock()
    api.async_get = AsyncMock(return_value={"data": {"items": []}})
    with patch("tools.orders.get_api", return_value=api):
        await get_orders(order_no="Vilas", limit=20, page=1)
    api.async_get.assert_awaited_once_with(
        "/commerce/orders",
        params={"limit": 20, "page": 1, "search": "Vilas"},
    )


@pytest.mark.asyncio
async def test_search_orders_filters_normalized_email_results():
    response = {
        "data": {
            "items": [
                _order({"email": "vilas@adaptnxt.com"}),
                _order({"email": "other@example.com"}, order_id="order-2"),
            ],
            "metadata": {"totalItems": 2},
        }
    }
    with patch("tools.orders.get_orders", new=AsyncMock(return_value=response)):
        result = await search_orders(customer_email=" VILAS@ADAPTNXT.COM ")
    assert result["found"] is True
    assert [order["id"] for order in result["orders"]] == ["order-1"]


@pytest.mark.asyncio
async def test_customer_context_combines_all_grouped_record_orders():
    candidate_response = {
        "data": {
            "items": [
                _order({"id": "record-1", "email": "vilas@adaptnxt.com"}),
                _order({"id": "record-2", "email": "VILAS@ADAPTNXT.COM"}, "order-2"),
            ]
        }
    }
    order_responses = [
        candidate_response,
        {"data": {"items": [_order({"id": "record-1"}, "order-1", 100)], "metadata": {"totalItems": 1}}},
        {"data": {"items": [_order({"id": "record-2"}, "order-2", 200)], "metadata": {"totalItems": 1}}},
        {"data": {"items": [_order({"id": "record-1"}, "order-1", 100)], "metadata": {"totalItems": 1}}},
        {"data": {"items": [_order({"id": "record-2"}, "order-2", 200)], "metadata": {"totalItems": 1}}},
    ]
    with patch(
        "tools.customers.get_orders",
        new=AsyncMock(side_effect=order_responses),
    ):
        result = await get_customer_context(email="vilas@adaptnxt.com")
    assert result["found"] is True
    assert result["statistics"]["orders_in_page"] == 2
    assert result["statistics"]["order_value_in_page"] == 300
    assert result["statistics"]["total_orders"] == 2
    assert result["statistics"]["total_order_value"] == 300


@pytest.mark.asyncio
async def test_customer_context_history_totals_and_return_count_span_records():
    candidate_response = {
        "data": {
            "items": [
                _order({"id": "record-1", "email": "vilas@adaptnxt.com"}),
                _order({"id": "record-2", "email": "VILAS@ADAPTNXT.COM"}, "order-2"),
            ]
        }
    }
    history = {
        "record-1": [
            _order({"id": "record-1"}, "r1-1", 100) | {
                "orderDate": "2026-01-02", "orderStatus": "FULFILLED"
            },
            _order({"id": "record-1"}, "r1-2", 200) | {
                "orderDate": "2026-01-03", "orderStatus": "RETURN_RECEIVED"
            },
        ],
        "record-2": [
            _order({"id": "record-2"}, "r2-1", 300) | {
                "orderDate": "2026-01-04", "orderStatus": "DELIVERED"
            },
        ],
    }

    async def fetch(**kwargs):
        if not kwargs.get("customer_id"):
            return candidate_response
        records = history[kwargs["customer_id"]]
        if kwargs["limit"] == 20:
            records = records[:1]
        return {
            "data": {
                "items": records,
                "metadata": {"totalItems": len(history[kwargs["customer_id"]])},
            }
        }

    with patch("tools.customers.get_orders", new=AsyncMock(side_effect=fetch)):
        result = await get_customer_context(email="vilas@adaptnxt.com")

    statistics = result["statistics"]
    assert statistics["orders_in_page"] == 2
    assert statistics["total_orders"] == 3
    assert statistics["total_order_value"] == 600
    assert statistics["status_counts"] == {
        "DELIVERED": 1, "FULFILLED": 1, "RETURN_RECEIVED": 1
    }
    assert statistics["first_order_date"] == "2026-01-02"
    assert statistics["last_order_date"] == "2026-01-04"
    assert statistics["product_returns"] == 1
    assert statistics["totals_are_partial"] is False
    assert "orders_returned" not in statistics
    assert "order_value_returned" not in statistics


@pytest.mark.asyncio
async def test_customer_history_page_cap_marks_totals_partial():
    page = {
        "data": {
            "items": [_order({"id": "record-1"}, f"order-{index}", 1) for index in range(100)],
            "metadata": {"totalItems": 1100},
        }
    }
    with patch("tools.customers.get_orders", new=AsyncMock(return_value=page)):
        from tools.customers import _customer_history

        _, total_orders, partial = await _customer_history(["record-1"])

    assert total_orders == 1100
    assert partial is True
