"""Tests for read-only business assistant views."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tools.business import (
    _shipment_follow_up,
    get_attention_queue,
    get_business_summary,
)


async def main() -> None:
    order_result = {"data": {"items": [{"orderNo": "1503", "orderStatus": "PENDING", "grandTotal": 100}]}}
    with patch("tools.business.get_orders", new=AsyncMock(return_value=order_result)), \
         patch("tools.business.get_order_stats", new=AsyncMock(return_value={"data": {"pending": 1}})), \
         patch("tools.business.get_product_summary", new=AsyncMock(return_value={"data": {"active": 1}})):
        summary = await get_business_summary("2026-09-30")
        assert summary["orders"]["count"] == 1
        assert summary["orders"]["order_value"] == 100
        assert "profit" in summary["data_notes"][0]

    with patch("tools.business.get_orders", new=AsyncMock(return_value=order_result)), \
         patch("tools.business.get_fulfillment_queue", new=AsyncMock(return_value={"orders": [{"order_number": "1503"}]})), \
         patch("tools.business.search_shipments", new=AsyncMock(return_value={"shipments": []})), \
         patch("tools.business.search_returns", new=AsyncMock(return_value={"returns": []})):
        queue = await get_attention_queue("2026-09-30")
        assert queue["found"]
        assert queue["items"][0]["category"] == "pending_orders"
        assert "evidence" in queue["items"][0]
    print("Business assistant tests passed")


@pytest.mark.asyncio
async def test_attention_queue_uses_combined_return_total():
    with patch("tools.business.get_orders", new=AsyncMock(return_value={"data": {"items": []}})), \
         patch("tools.business.get_fulfillment_queue", new=AsyncMock(return_value={"orders": []})), \
         patch("tools.business.search_shipments", new=AsyncMock(return_value={"shipments": []})), \
         patch("tools.business.search_returns", new=AsyncMock(return_value={
             "returns": [{"order_number": "1000000001"}],
             "pagination": {"totalItems": 15},
         })):
        queue = await get_attention_queue("2026-09-30")

    returns = next(item for item in queue["items"] if item["category"] == "returns")
    assert returns["evidence"]["count"] == 15
    assert returns["evidence"]["returns"] == ["1000000001"]
    assert all(value is not None for value in returns["evidence"]["returns"])


@pytest.mark.asyncio
async def test_attention_queue_shipment_follow_up_uses_order_date_fallback():
    shipments = {
        "shipments": [
            {
                "order_number": "1000000657",
                "order_status": "CONFIRMED",
                "order_date": "2026-10-09T00:00:00Z",
                "shipments": [{"id": "s1", "shipStatus": "CREATED", "awbNo": None}],
            },
            {
                "order_number": "1000000001",
                "order_status": "CONFIRMED",
                "order_date": "2026-10-06T00:00:00Z",
                "shipments": [{"id": "s2", "shipStatus": "CREATED", "awbNo": None}],
            },
            {
                "order_number": "delivered",
                "order_status": "CONFIRMED",
                "order_date": "2026-10-01T00:00:00Z",
                "shipments": [{"shipStatus": "DELIVERED"}],
            },
        ],
        "pagination": {"totalItems": 3},
    }
    with patch("tools.business.search_shipments", new=AsyncMock(return_value=shipments)):
        result = await _shipment_follow_up()
    assert [item["order_number"] for item in result["items"]] == [
        "1000000657",
        "1000000001",
    ]


@pytest.mark.asyncio
async def test_attention_queue_includes_empty_shipment_follow_up_category():
    with patch("tools.business.get_orders", new=AsyncMock(return_value={"data": {"items": []}})), \
         patch("tools.business.get_fulfillment_queue", new=AsyncMock(return_value={"orders": []})), \
         patch("tools.business.search_shipments", new=AsyncMock(return_value={"shipments": []})), \
         patch("tools.business.search_returns", new=AsyncMock(return_value={"returns": []})):
        queue = await get_attention_queue("2026-09-30")
    shipment = next(item for item in queue["items"] if item["category"] == "shipment_follow_up")
    assert shipment["evidence"]["total_count"] == 0


@pytest.mark.asyncio
async def test_attention_queue_uses_pagination_totals_and_samples():
    orders = {
        "data": {
            "items": [{"orderNo": f"day-{index}"} for index in range(12)],
            "metadata": {"totalItems": 20},
        }
    }
    fulfillment = {
        "orders": [{"order_number": f"pending-{index}"} for index in range(12)],
        "pagination": {"totalItems": 316},
    }
    shipment_pages = [
        {
            "shipments": [
                {
                    "order_number": "pending-shipment",
                    "order_status": "PENDING",
                    "shipments": [{
                        "shipStatus": "SHIPPED",
                        "deliveredAt": None,
                        "createdAt": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
                    }],
                },
                {
                    "order_number": "follow-up",
                    "order_status": "CONFIRMED",
                    "shipments": [{
                        "shipStatus": "SHIPPED",
                        "deliveredAt": None,
                        "createdAt": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
                    }],
                },
                {
                    "order_number": "follow-up-new",
                    "order_status": "CONFIRMED",
                    "shipments": [{
                        "shipStatus": "SHIPPED",
                        "deliveredAt": None,
                        "createdAt": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
                    }],
                },
                {
                    "order_number": "fresh",
                    "order_status": "CONFIRMED",
                    "shipments": [{
                        "shipStatus": "SHIPPED",
                        "deliveredAt": None,
                        "createdAt": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                    }],
                },
                {
                    "order_number": "delivered",
                    "order_status": "CONFIRMED",
                    "shipments": [{"shipStatus": "DELIVERED", "deliveredAt": "2026-01-01"}],
                },
            ],
            "pagination": {"totalItems": 5},
        }
    ]
    with patch("tools.business.get_orders", new=AsyncMock(return_value=orders)), \
         patch("tools.business.get_fulfillment_queue", new=AsyncMock(return_value=fulfillment)), \
         patch("tools.business.search_shipments", new=AsyncMock(side_effect=shipment_pages)), \
         patch("tools.business.search_returns", new=AsyncMock(return_value={"returns": []})):
        queue = await get_attention_queue("2026-09-30")

    by_category = {item["category"]: item["evidence"] for item in queue["items"]}
    assert by_category["pending_orders"]["total_count"] == 20
    assert by_category["pending_fulfillment"]["total_count"] == 316
    assert by_category["pending_fulfillment"]["sample_size"] == 10
    assert by_category["shipment_follow_up"]["orders"] == ["follow-up", "follow-up-new"]
    assert by_category["shipment_follow_up"]["total_count"] == 2


@pytest.mark.asyncio
async def test_shipment_scan_cap_marks_count_partial():
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    page = {
        "shipments": [{
            "order_number": f"old-{index}",
            "order_status": "CONFIRMED",
            "shipments": [{"shipStatus": "SHIPPED", "deliveredAt": None, "createdAt": old}],
        } for index in range(100)],
        "pagination": {"totalItems": 2000},
    }
    with patch("tools.business.search_shipments", new=AsyncMock(return_value=page)):
        result = await _shipment_follow_up()

    assert result["total_count_is_partial"] is True
    assert len(result["items"]) == 1000


if __name__ == "__main__":
    asyncio.run(main())
