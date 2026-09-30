"""Tests for customer views built on the existing orders service."""

import asyncio
from unittest.mock import AsyncMock, patch

from tools.customers import find_customers, get_customer_context


def order(customer: dict, order_id: str, number: str, total: float) -> dict:
    return {
        "id": order_id,
        "orderNo": number,
        "orderStatus": "FULFILLED",
        "grandTotal": total,
        "customer": customer,
        "orderItems": [{"name": "Book", "quantityOrdered": 1}],
    }


async def main() -> None:
    response = {
        "data": {
            "items": [
                order({"id": "c1", "firstName": "Krishna", "lastName": "Saraf", "email": "k@example.com"}, "o1", "1001", 500),
                order({"id": "c1", "firstName": "Krishna", "lastName": "Saraf", "email": "k@example.com"}, "o2", "1002", 250),
            ],
            "metadata": {"totalItems": 2},
        }
    }
    with patch("tools.customers.get_orders", new=AsyncMock(return_value=response)) as get_orders:
        found = await find_customers(name="Krishna Saraf")
        assert found["found"] and not found["ambiguous"]
        assert len(found["candidates"]) == 1

        context = await get_customer_context(customer_id="c1")
        assert context["customer"]["id"] == "c1"
        assert context["statistics"]["order_value_returned"] == 750
        assert get_orders.await_count == 3

    ambiguous = {
        "data": {
            "items": [
                order({"id": "c1", "name": "Alex Smith"}, "o1", "1", 1),
                order({"id": "c2", "name": "Alex Smith"}, "o2", "2", 2),
            ]
        }
    }
    with patch("tools.customers.get_orders", new=AsyncMock(return_value=ambiguous)):
        result = await get_customer_context(name="Alex Smith")
        assert result["ambiguous"] is True
        assert len(result["candidates"]) == 2

    print("Customer tests passed")


if __name__ == "__main__":
    asyncio.run(main())
