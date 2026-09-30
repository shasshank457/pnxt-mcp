"""Tests for product search and backend-owned inventory views."""

import asyncio
from unittest.mock import AsyncMock, patch

from tools.products import get_inventory_summary, get_product, search_inventory, search_products


PRODUCT = {
    "id": "p1", "sku": "SKU-1", "name": "Test Product", "status": "ACTIVE",
    "stocks": {"onHand": 10, "reserved": 3, "available": 7},
}


async def main() -> None:
    api = AsyncMock()
    api.async_get.side_effect = [
        {"data": {"items": [PRODUCT], "metadata": {"totalItems": 1}}},
        {"data": PRODUCT},
        {"data": {"items": [PRODUCT], "metadata": {"totalItems": 1}}},
        {"data": {"totalProducts": 1, "inventory": {"onHand": 10, "available": 7}}},
    ]
    with patch("tools.products.get_api", return_value=api):
        products = await search_products(sku="SKU-1")
        assert products["found"] and products["products"][0]["sku"] == "SKU-1"
        detail = await get_product("p1")
        assert detail["product"]["id"] == "p1"
        inventory = await search_inventory(sku="SKU-1")
        assert inventory["inventory"][0]["inventory_available"] is True
        summary = await get_inventory_summary()
        assert summary["summary"]["inventory"]["available"] == 7
    print("Product and inventory tests passed")


if __name__ == "__main__":
    asyncio.run(main())
