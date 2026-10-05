"""Tests for shipment and fulfillment views backed by orders."""

import asyncio
from unittest.mock import AsyncMock, patch

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
        result = await search_shipments(order_reference="1503")
        assert result["found"] and result["shipments"][0]["tracking_refresh"] == "not_performed"
        detail = await get_order_shipments("#1503")
        assert detail["found"] and "live refresh" in detail["message"]
        queue = await get_fulfillment_queue()
        assert queue["found"] and queue["work_queue_supported"] is False
    print("Shipment tests passed")


if __name__ == "__main__":
    asyncio.run(main())
