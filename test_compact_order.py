import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.orders import get_order, get_order_by_id

RAW_ORDER = {
    "id": "order-1082",
    "orderNo": "1000001082",
    "channelOrderNo": "CH-1082",
    "orderDate": "2026-01-10",
    "orderStatus": "SHIPPED",
    "tenantId": "tenant-secret",
    "version": 4,
    "sellerId": "seller-internal",
    "addressId": "address-internal",
    "customer": {
        "id": "customer-1",
        "firstName": "Vilas",
        "lastName": "Katigar",
        "email": "vilas@adaptnxt.com",
        "phone": "+919380027066",
        "state": "Karnataka",
        "tenantId": "tenant-secret",
        "role": "CUSTOMER",
        "username": "vilas",
        "createdAt": "internal",
    },
    "orderItems": [{
        "name": "84K",
        "sku": "84K",
        "quantityOrdered": 1,
        "quantityShipped": 1,
        "quantityReturned": 0,
        "itemStatus": "SHIPPED",
        "unitPrice": 250,
        "lineTotal": 250,
        "taxAmount": 0,
        "discountAmount": 0,
        "shippingAmount": 0,
        "codFee": 0,
        "giftWrapAmount": 0,
        "orderId": "internal",
        "product": {
            "stocks": [{
                "binLocation": "internal",
                "aisle": "A" * 100,
                "rack": "R" * 100,
                "shelf": "S" * 100,
                "bin": "B" * 100,
                "zone": "Z" * 100,
            }],
        },
    }],
    "paymentMethod": "COD",
    "grandTotal": 250,
    "paidAmount": 0,
    "refundAmount": 0,
    "currency": "INR",
    "warehouse": {
        "id": "warehouse-1",
        "code": "BLR",
        "name": "Bangalore",
        "tenantId": "tenant-secret",
        "config": {
            "fields": "sku,quantity,price,warehouse,bin,zone,aisle,rack,shelf",
            "internal_mapping": "x" * 400,
        },
    },
    "addressValidationStatus": "VALID",
    "shipments": [{
        "awbNo": "5129110116034",
        "shipStatus": "READY_FOR_PICKUP",
        "pieceCount": 1,
        "weightGrams": 500,
        "codCollectableAmount": 250,
        "isReturn": False,
        "createdAt": "2026-01-10T10:00:00Z",
        "deliveredAt": None,
        "courierId": "internal",
        "channelShipmentId": "internal",
    }],
    "returnedAt": None,
    "returnReason": None,
    "createdAt": "internal",
    "updatedAt": "internal",
}


@pytest.mark.asyncio
async def test_get_order_shapes_a_compact_support_response():
    api = MagicMock()
    api.async_get = AsyncMock(side_effect=[
        {"data": {"items": [{"id": "order-1082"}]}},
        {"data": RAW_ORDER},
    ])
    with patch("tools.orders.get_api", return_value=api):
        result = await get_order("1000001082")

    order = result["order"]
    assert order["customer"] == {
        "id": "customer-1",
        "name": "Vilas Katigar",
        "email": "vilas@adaptnxt.com",
        "phone": "+919380027066",
        "state": "Karnataka",
    }
    assert order["items"] == [{
        "name": "84K",
        "sku": "84K",
        "quantity_ordered": 1,
        "quantity_shipped": 1,
        "quantity_returned": 0,
        "item_status": "SHIPPED",
        "unit_price": 250,
        "line_total": 250,
    }]
    assert order["payment"] == {
        "method": "COD", "total": 250, "paid": 0, "refunded": 0, "currency": "INR"
    }
    assert order["fulfillment"] == {
        "warehouse": {"id": "warehouse-1", "code": "BLR", "name": "Bangalore"},
        "address_validation_status": "VALID",
    }
    assert order["shipments"][0]["awb_no"] == "5129110116034"
    assert order["shipments"][0]["delivered_at"] is None
    assert order["returns"] == {}

    serialized = json.dumps(order)
    assert len(serialized) < len(json.dumps(RAW_ORDER)) / 2
    for forbidden in ("tenantId", "config", "stocks", "binLocation", "version", "sellerId", "addressId", "courierId", "createdAt", "updatedAt"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_get_order_by_id_remains_raw():
    api = MagicMock()
    raw_response = {"data": RAW_ORDER}
    api.async_get = AsyncMock(return_value=raw_response)
    with patch("tools.orders.get_api", return_value=api):
        result = await get_order_by_id("order-1082")

    assert result is raw_response
    assert result["data"]["tenantId"] == "tenant-secret"
