"""Tests for return views built on the existing orders service."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tools.returns import get_order_returns, search_returns

ORDER = {
    "id": "o1", "orderNo": "1503", "orderStatus": "RETURN_REQUESTED",
    "customer": {"id": "c1", "firstName": "Krishna", "lastName": "Saraf"},
    "returnReason": "Damaged", "orderItems": [{"id": "i1", "name": "Book", "quantityOrdered": 1}],
}


async def main() -> None:
    api = AsyncMock(return_value={"data": {"items": [ORDER], "metadata": {"totalItems": 1}}})
    with patch("tools.returns.get_orders", new=api):
        result = await search_returns(return_status="RETURN_REQUESTED")
        assert result["found"] and result["returns"][0]["items"][0]["quantity_ordered"] == 1
        assert "refund_execution_supported" not in result

    with patch("tools.returns.get_orders", new=AsyncMock(return_value={"data": {"items": [ORDER]}})):
        order_returns = await get_order_returns("#1503")
        assert order_returns["found"]

    print("Return tests passed")


if __name__ == "__main__":
    asyncio.run(main())


@pytest.mark.asyncio
async def test_search_returns_without_status_merges_all_statuses():
    statuses = {
        "RETURN_REQUESTED": [{"id": "requested", "orderDate": "2026-01-01"}],
        "RETURN_RECEIVED": [{"id": "received", "orderDate": "2026-01-02"}],
        "PARTIALLY_RETURNED": [{"id": "partial", "orderDate": "2026-01-03"}],
        "RETURNED": [{"id": "returned", "orderDate": "2026-01-04"}],
        "REFUNDED": [{"id": "refunded", "orderDate": "2026-01-05"}],
        "PARTIALLY_REFUNDED": [{"id": "partial-refund", "orderDate": "2026-01-06"}],
    }

    async def fetch(**kwargs):
        return {"data": {"items": statuses[kwargs["order_status"]]}}

    with patch("tools.returns.get_orders", new=AsyncMock(side_effect=fetch)) as api:
        result = await search_returns(limit=20)

    assert result["pagination"]["totalItems"] == 6
    assert [item["order_id"] for item in result["returns"]] == [
        "partial-refund", "refunded", "returned", "partial", "received", "requested"
    ]
    assert {call.kwargs["order_status"] for call in api.call_args_list} == set(statuses)


@pytest.mark.asyncio
async def test_search_returns_single_status_preserves_backend_pagination():
    response = {"data": {"items": [ORDER], "metadata": {"page": 2, "limit": 5, "totalItems": 7}}}
    with patch("tools.returns.get_orders", new=AsyncMock(return_value=response)) as api:
        result = await search_returns(limit=5, page=2, return_status="RETURNED")

    assert result["pagination"] == response["data"]["metadata"]
    api.assert_awaited_once_with(
        limit=5, page=2, order_no=None, order_status="RETURNED", customer_id=None, warehouse_id=None
    )


@pytest.mark.asyncio
async def test_search_returns_invalid_status_lists_valid_values():
    with pytest.raises(ValueError) as error:
        await search_returns(return_status="NOT_A_STATUS")
    assert all(status in str(error.value) for status in (
        "RETURN_REQUESTED", "RETURN_RECEIVED", "PARTIALLY_RETURNED",
        "RETURNED", "REFUNDED", "PARTIALLY_REFUNDED",
    ))


@pytest.mark.asyncio
async def test_search_returns_merged_pagination():
    statuses = {
        status: [{"id": f"{status}-{index}", "orderDate": f"2026-01-{index:02d}"} for index in range(1, 3)]
        for status in ("RETURN_REQUESTED", "RETURN_RECEIVED", "PARTIALLY_RETURNED", "RETURNED", "REFUNDED", "PARTIALLY_REFUNDED")
    }

    async def fetch(**kwargs):
        return {"data": {"items": statuses[kwargs["order_status"]]}}

    with patch("tools.returns.get_orders", new=AsyncMock(side_effect=fetch)):
        result = await search_returns(limit=5, page=2)

    assert result["pagination"]["totalItems"] == 12
    assert len(result["returns"]) == 5


@pytest.mark.asyncio
async def test_order_status_return_view_does_not_invent_return_data():
    order = {
        **ORDER,
        "returnedAt": "2026-01-01",
        "returnReceivedAt": None,
        "shipments": [{
            "isReturn": True,
            "awbNo": "return-awb",
            "shipStatus": "IN_TRANSIT",
            "createdAt": "2026-01-02",
            "deliveredAt": None,
        }],
        "orderItems": [{"name": "Book", "quantityOrdered": 1, "quantityReturned": 0}],
    }
    with patch("tools.returns.get_orders", new=AsyncMock(return_value={"data": {"items": [order]}})):
        result = await get_order_returns("1503")

    view = result["returns"][0]
    assert "return_id" not in view
    assert view["data_source"] == "order_status"
    assert "Return data is derived from order status." in view["note"]
    assert "requested_at" not in view
    assert "received_at" not in view
    assert view["items"] == [{"name": "Book", "quantity_ordered": 1}]
    assert view["return_shipments"][0]["awb_no"] == "return-awb"
