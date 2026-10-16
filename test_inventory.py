"""Tests for product search and backend-owned inventory views."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tools.products import (
    get_inventory_summary,
    get_product,
    get_product_summary,
    search_inventory,
    search_products,
)

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


@pytest.mark.asyncio
async def test_product_inventory_and_metadata_are_compactly_mapped():
    product = {
        "id": "p1",
        "sku": "TFK356507408",
        "name": "Test Product",
        "status": "ACTIVE",
        "inventory": None,
        "stocks": [{
            "warehouse": {"code": "OSW"},
            "quantityOnHand": 99,
            "quantityAvailable": 90,
            "quantityReserved": 9,
        }],
        "brand": {"name": "Brand One"},
        "categoryName": "Category One",
        "vendor": {"name": "Vendor One"},
    }
    with patch("tools.products.get_api") as get_api:
        get_api.return_value.async_get = AsyncMock(
            return_value={"data": {"items": [product], "metadata": {"totalItems": 1}}}
        )
        result = await search_products(sku="TFK356507408")

    mapped = result["products"][0]
    assert mapped["inventory"] == {
        "tracked": True,
        "on_hand": 99,
        "available": 90,
        "reserved": 9,
        "by_warehouse": [{
            "warehouse_code": "OSW",
            "on_hand": 99,
            "available": 90,
            "reserved": 9,
        }],
    }
    assert mapped["brand"] == "Brand One"
    assert mapped["category"] == "Category One"
    assert mapped["vendor"] == "Vendor One"
    assert result["count_basis"]


@pytest.mark.asyncio
async def test_product_inventory_sums_multiple_warehouses_and_unmanaged_products():
    managed = {
        "id": "managed",
        "stocks": [
            {"warehouseCode": "A", "onHand": 10, "available": 8, "reserved": 2},
            {"warehouseCode": "B", "onHand": 5, "available": 4, "reserved": 1},
        ],
    }
    unmanaged = {"id": "unmanaged", "inventory": {"tracked": False}}
    from tools.products import _compact_product

    assert _compact_product(managed)["inventory"]["on_hand"] == 15
    assert _compact_product(managed)["inventory"]["available"] == 12
    assert _compact_product(unmanaged)["inventory"] == {"tracked": False}


@pytest.mark.asyncio
async def test_product_list_falls_back_to_detail_inventory_without_crashing():
    listed = {
        "id": "p-real",
        "sku": "TFK356507408",
        "name": "84K",
        "status": "ACTIVE",
    }
    detail = {
        "id": "p-real",
        "sku": "TFK356507408",
        "stocks": [
            {"warehouse": None, "warehouseCode": "OSW", "quantityOnHand": "99", "quantityAvailable": "90", "quantityReserved": "9"},
            None,
            {"warehouse": "OSW", "quantityOnHand": None},
        ],
    }
    api = AsyncMock()
    api.async_get.side_effect = [
        {"data": {"items": [listed], "metadata": {"totalItems": 1}}},
        {"data": detail},
    ]
    with patch("tools.products.get_api", return_value=api):
        result = await search_products(sku="TFK356507408", limit=3)

    assert result["products"][0]["inventory"]["on_hand"] == 99
    assert result["products"][0]["inventory"]["by_warehouse"][0]["warehouse_code"] == "OSW"


def test_missing_inventory_is_unknown_not_unmanaged():
    from tools.products import _compact_product

    result = _compact_product({"id": "unknown", "sku": "SCX9857"})
    assert result["inventory"] is None
    assert "unknown" in result["inventory_note"]


@pytest.mark.asyncio
async def test_product_summary_has_count_basis():
    with patch("tools.products.get_api") as get_api:
        get_api.return_value.async_get = AsyncMock(return_value={"data": {"total": 244}})
        result = await get_product_summary()

    assert result["data"]["total"] == 244
    assert result["count_basis"]
