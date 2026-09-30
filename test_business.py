"""Tests for read-only business assistant views."""

import asyncio
from unittest.mock import AsyncMock, patch

from tools.business import get_attention_queue, get_business_summary


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


if __name__ == "__main__":
    asyncio.run(main())
