"""Operational capabilities whose PointNXT endpoints are not currently exposed."""

from typing import Any

from services.tool_errors import unsupported_operation
from services.validation import optional_id, optional_text
from tools.orders import get_order


async def get_courier_rates(
    order_reference: str | None = None,
    shipment_id: str | None = None,
    origin_postal_code: str | None = None,
    destination_postal_code: str | None = None,
    weight: float | None = None,
) -> dict[str, Any]:
    """Fail closed because no courier-rate service is exposed by the backend."""
    optional_text(order_reference, "order_reference")
    optional_id(shipment_id, "shipment_id")
    optional_text(origin_postal_code, "origin_postal_code")
    optional_text(destination_postal_code, "destination_postal_code")
    if weight is not None and (isinstance(weight, bool) or weight <= 0):
        raise ValueError("weight must be a positive number")
    if not any((order_reference, shipment_id, origin_postal_code, destination_postal_code)):
        raise ValueError("Provide order_reference, shipment_id, or origin/destination postal codes")
    raise unsupported_operation(
        "a courier-rate endpoint or carrier-rate integration",
        action="get_courier_rates",
        details={"rates": [], "read_only_request": True},
    )


async def retry_delivery(
    order_reference: str,
    shipment_id: str | None = None,
    reason: str | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Validate order existence, then fail closed because shipment retry is unsupported."""
    optional_text(order_reference, "order_reference", required=True)
    optional_id(shipment_id, "shipment_id")
    optional_text(reason, "reason")
    optional_text(confirmation_token, "confirmation_token")
    optional_text(idempotency_key, "idempotency_key")
    order_result = await get_order(order_reference)
    if not order_result.get("found"):
        raise ValueError("Order was not found")
    order = order_result.get("order") or {}
    shipments = order.get("shipments") or []
    if shipment_id:
        shipments = [s for s in shipments if str(s.get("id") or s.get("shipmentId")) == shipment_id]
        if not shipments:
            raise ValueError("Shipment was not found on the order")
    if not confirm:
        return {
            "status": "preview",
            "action": "retry_delivery",
            "backend_capability": False,
            "confirmation_required": True,
            "current_shipments": shipments,
            "message": "Preview only; explicit confirmation would be required, but no retry endpoint is available.",
        }
    raise unsupported_operation("a shipment delivery-retry endpoint", action="retry_delivery", details={"confirmation_required": True})


async def update_support_ticket(
    ticket_id: str,
    status: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Fail closed because ticket creation and update integrations are absent."""
    optional_id(ticket_id, "ticket_id")
    optional_text(status, "status")
    optional_text(subject, "subject")
    optional_text(description, "description")
    optional_text(idempotency_key, "idempotency_key")
    if not any((status, subject, description)):
        raise ValueError("Provide at least one permitted ticket field to update")
    raise unsupported_operation("a support-ticket backend and ticket-update endpoint", action="update_support_ticket")


async def add_order_note(
    order_reference: str,
    note: str,
    visibility: str = "internal",
    confirm: bool = False,
    confirmation_token: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Validate the order and note visibility, then fail closed without persistence."""
    optional_text(order_reference, "order_reference", required=True)
    optional_text(note, "note", required=True)
    optional_text(visibility, "visibility", required=True)
    optional_text(confirmation_token, "confirmation_token")
    optional_text(idempotency_key, "idempotency_key")
    if visibility.lower() not in {"internal", "customer"}:
        raise ValueError("visibility must be 'internal' or 'customer'")
    order_result = await get_order(order_reference)
    if not order_result.get("found"):
        raise ValueError("Order was not found")
    raise unsupported_operation(
        "an order-note persistence endpoint",
        action="add_order_note",
        details={"order_reference": order_reference.strip().lstrip("#"), "visibility": visibility.lower(), "confirmation_required": visibility.lower() == "customer"},
    )
