"""Async Products checks using a mocked lazy PointNXT API client."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from tools import products as products_module
from tools.products import get_product_by_id, get_product_summary, get_products


def product_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return product items from an awaited product response."""

    return response.get("data", {}).get("items", [])


def mock_products_api() -> MagicMock:
    """Build an async API mock so tests never require authentication."""

    product = {"id": "product-1", "sku": "SKU-1", "name": "Test Product", "status": "ACTIVE"}
    api = MagicMock()

    async def async_get(endpoint: str, params: dict | None = None) -> dict:
        if endpoint == "/commerce/products/summary":
            return {"data": {"totalProducts": 1, "activeProducts": 1}}
        if endpoint.startswith("/commerce/products/"):
            return {"data": product}
        return {"data": {"items": [product], "total": 1}}

    api.async_get = AsyncMock(side_effect=async_get)
    return api


async def run_test(
    name: str,
    operation: Callable[[], Awaitable[Any]],
    expect_error: bool = False,
) -> bool:
    """Run and report one awaited product operation."""

    try:
        result = operation()
        if inspect.isawaitable(result):
            await result
        passed = not expect_error
        print(f"{name} | PASS | request succeeded")
        return passed
    except Exception as error:  # noqa: BLE001 - test harness reports operation failures
        passed = expect_error
        print(f"{name} | {'PASS' if passed else 'FAIL'} | {error}")
        return passed


async def main() -> int:
    """Run all Products checks against the mocked lazy API."""

    api = mock_products_api()
    with patch.object(products_module, "get_api", return_value=api):
        latest = await get_products(limit=5, page=1)
        items = product_items(latest)
        if not items:
            print("Product fixture setup | FAIL | no products returned")
            return 1

        sample = items[0]
        sample_id = sample["id"]
        operations = [
            ("Latest products", lambda: get_products(limit=5, page=1)),
            ("Pagination", lambda: get_products(limit=5, page=1)),
            ("Search by SKU", lambda: get_products(sku=sample.get("sku"))),
            ("Search by product name", lambda: get_products(name=sample.get("name"))),
            ("Filter by status", lambda: get_products(status="ACTIVE")),
            ("Filter by category", lambda: get_products(category_id="category-1")),
            ("Filter by brand/vendor", lambda: get_products(vendor_id="vendor-1")),
            ("Product by ID", lambda: get_product_by_id(sample_id)),
            ("Product summary", get_product_summary),
        ]
        results = [await run_test(name, operation) for name, operation in operations]
        results.append(
            await run_test(
                "Invalid product ID",
                lambda: get_product_by_id(""),
                expect_error=True,
            )
        )

    passed = sum(results)
    failed = len(results) - passed
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    return int(failed != 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
