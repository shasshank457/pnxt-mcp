"""Tests for shipment and fulfillment views backed by orders."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tools.shipments import get_fulfillment_queue, get_order_shipments, search_shipments

ORDER = {
    "id": "o1",
    "orderNo": "1503",
    "orderStatus": "PENDING",
    "channelOrderStatus": "UNFULFILLED",
    "shipments": [],
    "warehouse": {"id": "w1", "name": "Main Warehouse"},
}


async def main() -> None:
    response = {"data": {"items": [ORDER], "metadata": {"totalItems": 1}}}
    with patch("tools.shipments.get_orders", new=AsyncMock(return_value=response)):
        result = await search_shipments(
            order_reference="1503", include_without_shipments=True
        )
        assert result["found"] and result["shipments"][0]["tracking_refresh"] == "not_performed"
        detail = await get_order_shipments("#1503")
        assert detail["found"] and "live refresh" in detail["message"]
        queue = await get_fulfillment_queue()
        assert queue["found"] and queue["work_queue_supported"] is False
    print("Shipment tests passed")


if __name__ == "__main__":
    asyncio.run(main())


def _shipped_order(number: str, status: str = "READY_FOR_PICKUP") -> dict:
    return {
        "id": number,
        "orderNo": number,
        "shipments": [{"shipStatus": status, "awb": "5129110116034"}],
    }


@pytest.mark.asyncio
async def test_search_shipments_default_excludes_orders_without_shipments():
    response = {
        "data": {
            "items": [ORDER, _shipped_order("1000001082")],
            "metadata": {"totalItems": 2},
        }
    }
    with patch("tools.shipments.get_orders", new=AsyncMock(return_value=response)):
        result = await search_shipments()

    assert [item["order_number"] for item in result["shipments"]] == ["1000001082"]
    assert result["pagination"]["totalItems"] == 1
    assert result["shipments"][0]["shipment_data_available"] is True


@pytest.mark.asyncio
async def test_search_shipments_can_include_orders_without_shipments():
    response = {"data": {"items": [ORDER, _shipped_order("1000001082")]}}
    with patch("tools.shipments.get_orders", new=AsyncMock(return_value=response)):
        result = await search_shipments(include_without_shipments=True)

    assert len(result["shipments"]) == 2
    assert result["shipments"][0]["shipment_data_available"] is False


@pytest.mark.asyncio
async def test_search_shipments_filters_by_ship_status_and_paginates():
    response = {
        "data": {
            "items": [
                _shipped_order("ready", "READY_FOR_PICKUP"),
                _shipped_order("transit", "IN_TRANSIT"),
                _shipped_order("delivered", "DELIVERED"),
            ],
            "metadata": {"totalItems": 3},
        }
    }
    with patch("tools.shipments.get_orders", new=AsyncMock(return_value=response)):
        result = await search_shipments(ship_status="in_transit")

    assert [item["order_number"] for item in result["shipments"]] == ["transit"]


@pytest.mark.asyncio
async def test_search_shipments_accepts_created_status():
    response = {
        "data": {
            "items": [_shipped_order("created", "CREATED")],
            "metadata": {"totalItems": 1},
        }
    }
    with patch("tools.shipments.get_orders", new=AsyncMock(return_value=response)):
        result = await search_shipments(ship_status="CREATED")
    assert result["shipments"][0]["order_number"] == "created"


@pytest.mark.asyncio
async def test_search_shipments_scan_cap_marks_total_partial():
    response = {
        "data": {
            "items": [_shipped_order(str(index)) for index in range(100)],
            "metadata": {"totalItems": 2000},
        }
    }
    with patch("tools.shipments.get_orders", new=AsyncMock(return_value=response)):
        result = await search_shipments(limit=5, page=2)

    assert result["pagination"]["totalItems"] == 1000
    assert result["pagination"]["total_is_partial"] is True
    assert len(result["shipments"]) == 5
