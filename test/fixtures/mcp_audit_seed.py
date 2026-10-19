"""Deterministic API-shaped records for the MCP audit regression suite."""

AUDIT_SEED = {
    "tenant": {"id": "tenant-audit", "name": "Audit Tenant", "timezone": "Asia/Kolkata"},
    "warehouses": [
        {"id": "warehouse-mum", "code": "MUM-01", "name": "Mumbai"},
        {"id": "warehouse-del", "code": "DEL-01", "name": "Delhi"},
    ],
    "channels": [
        {
            "id": "channel-active",
            "name": "Active marketplace",
            "status": "ACTIVE",
            "suspendedReason": "Previously suspended",
            "suspendedAt": "2026-09-01T00:00:00Z",
        },
        {
            "id": "channel-suspended",
            "name": "Suspended marketplace",
            "status": "SUSPENDED",
            "suspendedReason": "Audit fixture suspension",
            "suspendedAt": "2026-10-01T00:00:00Z",
        },
    ],
    "customer": {
        "id": "customer-audit",
        "name": "Audit Customer",
        "email": "audit@example.test",
        "phone": "+919876543210",
        "totalOrders": 0,
        "totalSpent": 0,
    },
    "orders": [
        {
            "id": "order-woo",
            "orderNo": "AUDIT-100",
            "channelOrderNo": "WC-100",
            "orderDate": "2026-10-10T20:00:00Z",
            "createdAt": "2026-10-10T20:00:00Z",
            "orderStatus": "FULFILLED",
            "confirmedAt": None,
            "fulfilledAt": None,
            "customerId": "customer-audit",
            "customer": {
                **{
                    key: value
                    for key, value in {
                        "id": "customer-audit",
                        "name": "Audit Customer",
                        "email": "audit@example.test",
                        "phone": "+919876543210",
                        "channelCustomerId": "wc-customer-secret@woocommerce.test",
                    }.items()
                }
            },
            "grandTotal": 100.1,
            "paymentMethod": "COD",
            "currency": "INR",
            "config": {
                "woocommerce": {
                    "raw": {
                        "customer_ip_address": "203.0.113.45",
                        "payment_url": "https://pay.example.test/private",
                        "order_key": "wc_order_private_key",
                        "cart_hash": "private-cart-hash",
                        "billing": {
                            "first_name": "Audit",
                            "last_name": "Customer",
                            "address_1": "1 Billing Road",
                            "city": "Mumbai",
                            "postcode": "400001",
                        },
                        "shipping": {
                            "first_name": "Audit",
                            "last_name": "Customer",
                            "address_1": "2 Shipping Road",
                            "city": "Mumbai",
                            "postcode": "400002",
                        },
                    }
                }
            },
            "shippingAddressSnapshot": {
                "firstName": "Audit",
                "lastName": "Customer",
                "address1": "2 Shipping Road",
                "city": "Mumbai",
                "postcode": "400002",
            },
            "billingAddress": {
                "firstName": "Audit",
                "lastName": "Customer",
                "email": "audit@example.test",
                "phone": "+919876543210",
                "city": "Mumbai",
                "state": "Maharashtra",
            },
            "warehouse": {"id": "warehouse-mum", "code": "MUM-01", "name": "Mumbai"},
            "orderItems": [
                {
                    "id": "line-unmapped",
                    "name": "Unmapped item",
                    "productId": None,
                    "sku": None,
                    "quantityOrdered": 1,
                    "unitPrice": 100.1,
                }
            ],
            "shipments": [
                {
                    "id": "shipment-audit",
                    "awbNo": "AWB-AUDIT",
                    "shipStatus": "DELIVERED",
                    "createdAt": "2026-10-11T02:00:00Z",
                    "deliveredAt": "2026-10-12T02:00:00Z",
                }
            ],
        },
        {
            "id": "order-pending",
            "orderNo": "AUDIT-101",
            "orderDate": "2026-10-11T08:00:00Z",
            "createdAt": "2026-10-11T08:00:00Z",
            "orderStatus": "PENDING",
            "isTest": True,
            "tenantId": "tenant-audit",
            "customerId": "customer-audit",
            "customer": {
                "id": "customer-audit",
                "name": "Audit Customer",
                "email": "audit@example.test",
                "phone": "+919876543210",
            },
            "grandTotal": 200.2,
            "paymentMethod": "PREPAID",
            "currency": "INR",
            "orderItems": [],
            "shipments": [],
        },
        {
            "id": "order-confirmed",
            "orderNo": "AUDIT-102",
            "orderDate": "2026-10-12T08:00:00Z",
            "createdAt": "2026-10-12T08:00:00Z",
            "orderStatus": "CONFIRMED",
            "customerId": "customer-audit",
            "customer": {
                "id": "customer-audit",
                "name": "Audit Customer",
                "email": "audit@example.test",
                "phone": "+919876543210",
            },
            "grandTotal": 300.3,
            "paymentMethod": "PREPAID",
            "currency": "INR",
            "orderItems": [],
            "shipments": [],
        },
    ],
    "products": [
        {
            "id": "product-audit",
            "tenantId": "tenant-audit",
            "version": 3,
            "sku": "AUDIT-SKU",
            "name": "Weighted audit product",
            "description": "<p>Fixture product description</p>",
            "config": {"importedFrom": "woocommerce"},
            "status": "ACTIVE",
            "costPrice": 25.75,
            "weight": "0.344",
            "weightUnit": "kg",
            "stocks": [
                {
                    "warehouseId": "warehouse-mum",
                    "warehouseCode": "MUM-01",
                    "quantityOnHand": 5,
                    "quantityAvailable": 5,
                    "averageCost": 0,
                }
            ],
        }
    ],
    "wallet": {
        "id": "wallet-audit",
        "balance": 75,
        "ledger": [
            {"id": "ledger-credit", "type": "CREDIT", "amount": 100, "balanceAfter": 100},
            {"id": "ledger-debit", "type": "DEBIT", "amount": 25, "balanceAfter": 75},
        ],
    },
}


def response_for(endpoint: str, params: dict | None = None) -> dict:
    """Return deterministic backend envelopes used by registered MCP handlers."""
    params = params or {}
    if endpoint == "/commerce/orders":
        orders = AUDIT_SEED["orders"]
        if params.get("customerId"):
            orders = [o for o in orders if o.get("customerId") == params["customerId"]]
        if params.get("search"):
            search = str(params["search"]).casefold()
            orders = [
                order
                for order in orders
                if search
                in " ".join(
                    str(order.get(key, ""))
                    for key in ("orderNo", "channelOrderNo")
                ).casefold()
            ]
        if params.get("orderStatus"):
            orders = [
                order
                for order in orders
                if order.get("orderStatus") == params["orderStatus"]
            ]
        start = (int(params.get("page", 1)) - 1) * int(params.get("limit", 100))
        page_orders = orders[start : start + int(params.get("limit", 100))]
        return {
            "data": {
                "items": page_orders,
                "totalItems": len(orders),
                "metadata": {
                    "page": int(params.get("page", 1)),
                    "limit": int(params.get("limit", 100)),
                    "totalItems": len(orders),
                },
            }
        }
    if endpoint.startswith("/commerce/orders/") and endpoint.rsplit("/", 1)[-1] in {
        order["id"] for order in AUDIT_SEED["orders"]
    }:
        order_id = endpoint.rsplit("/", 1)[-1]
        return {"data": next(o for o in AUDIT_SEED["orders"] if o["id"] == order_id)}
    if endpoint == "/commerce/orders/stats":
        return {"data": {"totalOrders": 3, "pendingOrders": 1}}
    if endpoint == "/commerce/orders/status-transitions":
        return {"data": []}
    if endpoint == "/commerce/products":
        products = AUDIT_SEED["products"]
        return {
            "data": {
                "items": products,
                "totalItems": len(products),
                "metadata": {"page": 1, "limit": len(products), "totalItems": len(products)},
            }
        }
    if endpoint.startswith("/commerce/products/"):
        product_id = endpoint.rsplit("/", 1)[-1]
        product = next(
            (item for item in AUDIT_SEED["products"] if item["id"] == product_id),
            None,
        )
        return {"data": product or {}}
    return {"data": {"items": [], "totalItems": 0, "metadata": {"totalItems": 0}}}
