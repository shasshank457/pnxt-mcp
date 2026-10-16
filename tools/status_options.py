"""Shared backend status options exposed by the read-only MCP tools."""

ORDER_STATUSES = (
    "PENDING",
    "ON_HOLD",
    "CONFIRMED",
    "PARTIALLY_FULFILLED",
    "FULFILLED",
    "DELIVERED",
    "CANCELLED",
    "RETURN_REQUESTED",
    "RETURN_RECEIVED",
    "PARTIALLY_RETURNED",
    "RETURNED",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
    "DISPUTED",
    "CLOSED",
)

RETURN_STATUSES = (
    "RETURN_REQUESTED",
    "RETURN_RECEIVED",
    "PARTIALLY_RETURNED",
    "RETURNED",
    "REFUNDED",
    "PARTIALLY_REFUNDED",
)

# These are the shipment statuses used by the API responses and repository fixtures.
SHIP_STATUSES = ("READY_FOR_PICKUP", "IN_TRANSIT", "SHIPPED", "DELIVERED", "CREATED")
PRODUCT_STATUSES = ("ACTIVE", "DRAFT", "ARCHIVED")
