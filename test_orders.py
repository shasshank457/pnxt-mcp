"""Orders integration and validation checks.

Run all sections with ``python test_orders.py`` or one section with, for
example, ``python test_orders.py validation``.
"""

import asyncio
import inspect
import sys
from unittest.mock import patch

import requests

from services import pointnxt_api
from tools import orders as orders_module
from tools.orders import (
    get_order_by_id,
    get_order_stats,
    get_orders,
    get_orders_summary,
    get_status_transitions,
)


def is_tenant_context_error(error: Exception) -> bool:
    """Return whether an HTTP error contains the known tenant-context error."""

    response = getattr(error, "response", None)
    response_text = response.text if response is not None else str(error)
    return "tenant.error.tenantRequired" in response_text


async def test_validation() -> None:
    """Run invalid-input checks without making backend requests."""

    cases = {
        "invalid date format": {"start_date": "2024/01/01"},
        "invalid status": {"order_status": "NOT_A_STATUS"},
        "invalid page": {"page": 0},
        "invalid limit": {"limit": -1},
    }
    for name, filters in cases.items():
        with patch.object(orders_module, "get_api") as get_api:
            try:
                await get_orders(**filters)
            except ValueError as error:
                get_api.assert_not_called()
                print(f"SUCCESS: {name} - {error}")
            else:
                raise AssertionError(f"{name} did not raise ValueError")


def test_connection_failure() -> None:
    """Verify connection failures are converted into clear runtime errors."""

    with patch.object(
        pointnxt_api.requests,
        "request",
        side_effect=requests.ConnectionError("simulated connection failure"),
    ):
        try:
            pointnxt_api.PointNXTAPI().get(
                "/commerce/orders",
                params={"limit": 1, "page": 1},
            )
        except RuntimeError as error:
            print(f"SUCCESS: connection failure - {error}")
        else:
            raise AssertionError("connection failure was hidden")


async def test_order_retrieval() -> None:
    """Exercise common order retrieval queries and order details."""

    queries = {
        "latest orders": {},
        "cancelled orders": {"order_status": "CANCELLED"},
        "pending orders": {"order_status": "PENDING"},
        "orders between dates": {
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
    }
    latest = None
    for name, filters in queries.items():
        response = await get_orders(**filters)
        print(
            f"SUCCESS: {name} ({len(response.get('data', {}).get('items', []))} orders)"
        )
        if latest is None:
            latest = response

    items = latest.get("data", {}).get("items", []) if latest else []
    if items:
        try:
            await get_order_by_id(items[0]["id"])
            print("SUCCESS: order by ID")
        except Exception as error:
            if is_tenant_context_error(error):
                print("PENDING: get_order_by_id requires tenant context.")
            else:
                raise


async def test_summary() -> None:
    """Exercise the business-friendly order summary."""

    for name, filters in {
        "latest summary": {},
        "cancelled summary": {"order_status": "CANCELLED"},
        "date-range summary": {
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
    }.items():
        summary = await get_orders_summary(**filters)
        print(f"SUCCESS: {name}: {summary}")


async def test_metadata() -> None:
    """Exercise statistics and workflow metadata endpoints."""

    try:
        await get_order_stats()
        print("SUCCESS: order statistics")
    except Exception as error:
        if is_tenant_context_error(error):
            print("PENDING: get_order_stats requires tenant context.")
        else:
            raise
    await get_status_transitions()
    print("SUCCESS: status transitions")


async def main() -> None:
    """Run all sections or the section named on the command line."""

    sections = {
        "validation": test_validation,
        "connection": test_connection_failure,
        "retrieval": test_order_retrieval,
        "summary": test_summary,
        "metadata": test_metadata,
    }
    requested = sys.argv[1:] or list(sections)
    for name in requested:
        if name not in sections:
            raise SystemExit(
                f"Unknown section: {name}. Choose from {', '.join(sections)}"
            )
        print(f"\n--- {name} ---")
        result = sections[name]()
        if inspect.isawaitable(result):
            await result


if __name__ == "__main__":
    asyncio.run(main())
