"""Tests for return views built on the existing orders service."""

import asyncio
from unittest.mock import AsyncMock, patch

from tools.returns import get_order_returns, search_returns

ORDER = {
    "id": "o1", "orderNo": "1503", "orderStatus": "RETURN_REQUESTED",
    "customer": {"id": "c1", "firstName": "Krishna", "lastName": "Saraf"},
    "returnReason": "Damaged", "orderItems": [{"id": "i1", "name": "Book", "returnQuantity": 1}],
}


async def main() -> None:
    api = AsyncMock(return_value={"data": {"items": [ORDER], "metadata": {"totalItems": 1}}})
    with patch("tools.returns.get_orders", new=api):
        result = await search_returns(return_status="RETURN_REQUESTED")
        assert result["found"] and result["returns"][0]["items"][0]["requested_quantity"] == 1
        assert "refund_execution_supported" not in result

    with patch("tools.returns.get_orders", new=AsyncMock(return_value={"data": {"items": [ORDER]}})):
        order_returns = await get_order_returns("#1503")
        assert order_returns["found"]

    print("Return tests passed")


if __name__ == "__main__":
    asyncio.run(main())
