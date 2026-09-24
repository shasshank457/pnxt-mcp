"""Production integration checks for the Products MCP module."""

import io
import re
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

import requests

from tools.products import get_product_by_id, get_product_summary, get_products


def product_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return product items from a PointNXT response envelope."""

    return response.get("data", {}).get("items", [])


def run_test(
    name: str,
    operation: Callable[[], Any],
    expect_error: bool = False,
) -> bool:
    """Run one operation and print its URL, HTTP status, and result."""

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            operation()
        output = captured.getvalue()
        url_match = re.search(r"Request URL: (.+)", output)
        status_match = re.search(r"Response status code: (\d+)", output)
        url = url_match.group(1).strip() if url_match else "unknown"
        status = int(status_match.group(1)) if status_match else 200
        passed = (status >= 400) if expect_error else status == 200
        summary = "expected backend error" if expect_error else "request succeeded"
        print(
            f"{name} | URL: {url} | HTTP {status} | {'PASS' if passed else 'FAIL'} | {summary}"
        )
        return passed
    except requests.HTTPError as error:
        response = error.response
        status = response.status_code if response is not None else "unknown"
        passed = expect_error and isinstance(status, int) and status >= 400
        print(
            f"{name} | URL: {getattr(response, 'url', 'unknown')} | HTTP {status} | {'PASS' if passed else 'FAIL'} | backend error"
        )
        return passed
    except Exception as error:  # noqa: BLE001 - test harness reports all backend failures
        print(f"{name} | URL: unknown | HTTP unknown | FAIL | {error}")
        return False


results = []
latest: dict[str, Any] | None = None

results.append(run_test("Latest products", lambda: get_products(limit=5, page=1)))
latest = get_products(limit=5, page=1)
items = product_items(latest)

if not items:
    print(
        "Product fixture setup | URL: unknown | HTTP unknown | FAIL | no products returned"
    )
    sys.exit(1)

sample = items[0]
sample_id = sample["id"]

test_operations = [
    ("Pagination", lambda: get_products(limit=5, page=1)),
    ("Search by SKU", lambda: get_products(sku=sample.get("sku"))),
    ("Search by product name", lambda: get_products(name=sample.get("name"))),
    ("Filter by status", lambda: get_products(status=sample.get("status") or "ACTIVE")),
    ("Filter by category", lambda: get_products(category_id=sample.get("categoryId"))),
    ("Filter by brand/vendor", lambda: get_products(vendor_id=sample.get("vendorId"))),
    ("Product by ID", lambda: get_product_by_id(sample_id)),
    ("Product summary", get_product_summary),
    (
        "Invalid product ID",
        lambda: get_product_by_id("00000000-0000-0000-0000-000000000000"),
        True,
    ),
]

for operation in test_operations:
    if len(operation) == 3:
        name, function, expect_error = operation
        results.append(run_test(name, function, expect_error))
    else:
        name, function = operation
        results.append(run_test(name, function))

passed = sum(results)
failed = len(results) - passed
print(f"Passed: {passed}")
print(f"Failed: {failed}")
sys.exit(1 if failed else 0)
