import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from uuid import UUID, uuid4

from services.auth_session import get_session
from services.customer_matching import (
    normalize_email,
    normalize_phone,
)
from services.pointnxt_api import PointNXTAPI
from services.response_safety import sanitize_public_response
from services.timezone import local_date_period, local_timestamp, resolve_timezone
from services.write_tools import (
    ConfirmationTokenManager,
    InMemoryWriteStateStore,
    WriteContext,
    WriteError,
    map_backend_error,
    WriteToolSpec,
)
from tools.status_options import ORDER_STATUSES

logger = logging.getLogger(__name__)

_ORDER_WEIGHT_UNITS = frozenset({"GM", "KG", "LB", "OZ"})
_ORDER_MEASURE_UNITS = frozenset({"MM", "CM", "IN"})
_ORDER_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ADDRESS_REQUIRED = frozenset({"name", "phone", "address_line1", "city", "state_or_region", "postal_code", "country_code"})
_ADDRESS_OPTIONAL = frozenset({"company_legal_name", "email", "address_line2", "address_line3", "county", "district", "municipality", "taxing_region"})
_ITEM_ALLOWED = frozenset({"name", "quantity_ordered", "item_price", "code", "channel_sku", "item_image", "product_id"})


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_create_order_input(args: dict[str, Any], context: WriteContext) -> None:
    if not context.tenant_id:
        raise WriteError("AUTH_REQUIRED", "An authenticated tenant is required to create an order.")
    allowed = {"order_no", "order_date", "seller_id", "channel_id", "billing_address", "shipping_address", "items", "customer_id", "currency", "payment_method", "fulfillment_instruction", "order_weight", "weight_unit_code", "dimension_length", "dimension_breadth", "dimension_height", "measure_unit_code"}
    forbidden = {"tenantId", "tenant_id", "orderStatus", "order_status", "orderTotal", "order_total", "payment_state", "fulfillment_state"}
    unknown = set(args) - allowed - {"confirm", "confirmation_token", "idempotency_key"}
    if unknown or forbidden.intersection(args):
        raise WriteError("VALIDATION_ERROR", "Unexpected or forbidden create-order field supplied.")
    for name in ("order_no", "order_date"):
        if not isinstance(args.get(name), str) or not args[name].strip():
            raise WriteError("VALIDATION_ERROR", f"{name} must be a non-empty string.")
    try:
        datetime.fromisoformat(args["order_date"].replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise WriteError("VALIDATION_ERROR", "order_date must be an ISO date string.") from None
    for name in ("seller_id", "channel_id", "customer_id"):
        if args.get(name) is not None:
            try:
                UUID(args[name].strip())
            except (ValueError, AttributeError):
                raise WriteError("VALIDATION_ERROR", f"{name} must be a valid UUID.") from None
    for name in ("billing_address", "shipping_address"):
        address = args.get(name)
        if not isinstance(address, dict):
            raise WriteError("VALIDATION_ERROR", f"{name} must be an object.")
        unknown_address = set(address) - _ADDRESS_REQUIRED - _ADDRESS_OPTIONAL
        if unknown_address or any(not isinstance(address.get(field), str) or not address[field].strip() for field in _ADDRESS_REQUIRED):
            raise WriteError("VALIDATION_ERROR", f"{name} contains missing, invalid, or unexpected fields.")
        for field in _ADDRESS_OPTIONAL:
            if field in address and not isinstance(address[field], str):
                raise WriteError("VALIDATION_ERROR", f"{name}.{field} must be a string.")
        if "email" in address and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address["email"]):
            raise WriteError("VALIDATION_ERROR", f"{name}.email must be a valid email.")
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise WriteError("VALIDATION_ERROR", "items must be a non-empty array.")
    for item in items:
        if not isinstance(item, dict) or set(item) - _ITEM_ALLOWED:
            raise WriteError("VALIDATION_ERROR", "Each item contains invalid fields.")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise WriteError("VALIDATION_ERROR", "Each item name must be non-empty.")
        if not _finite_number(item.get("quantity_ordered")) or item["quantity_ordered"] <= 0:
            raise WriteError("VALIDATION_ERROR", "quantity_ordered must be finite and positive.")
        if not _finite_number(item.get("item_price")) or item["item_price"] < 0:
            raise WriteError("VALIDATION_ERROR", "item_price must be finite and non-negative.")
        for field in ("code", "channel_sku", "item_image"):
            if field in item and not isinstance(item[field], str):
                raise WriteError("VALIDATION_ERROR", f"{field} must be a string.")
        if item.get("product_id") is not None:
            try:
                UUID(item["product_id"])
            except (ValueError, TypeError):
                raise WriteError("VALIDATION_ERROR", "product_id must be a valid UUID.") from None
    if args.get("currency") is not None and (not isinstance(args["currency"], str) or not _ORDER_CURRENCY.fullmatch(args["currency"].upper())):
        raise WriteError("VALIDATION_ERROR", "currency must be a three-letter ISO 4217 code.")
    for name in ("payment_method", "fulfillment_instruction"):
        if args.get(name) is not None and not isinstance(args[name], str):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a string.")
    for name, allowed_values in (("weight_unit_code", _ORDER_WEIGHT_UNITS), ("measure_unit_code", _ORDER_MEASURE_UNITS)):
        if args.get(name) is not None and (not isinstance(args[name], str) or args[name].upper() not in allowed_values):
            raise WriteError("VALIDATION_ERROR", f"{name} is invalid.")
    if args.get("order_weight") is not None and (not _finite_number(args["order_weight"]) or args["order_weight"] <= 0):
        raise WriteError("VALIDATION_ERROR", "order_weight must be finite and positive.")
    for name in ("dimension_length", "dimension_breadth", "dimension_height"):
        if args.get(name) is not None and (not _finite_number(args[name]) or args[name] <= 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be finite and positive.")


def _create_order_payload(args: dict[str, Any]) -> dict[str, Any]:
    mapping = {"order_no": "orderNo", "order_date": "orderDate", "seller_id": "sellerId", "channel_id": "channelId", "customer_id": "customerId", "currency": "currency", "payment_method": "paymentMethod", "fulfillment_instruction": "fulfillmentInstruction", "order_weight": "orderWeight", "weight_unit_code": "weightUnitCode", "dimension_length": "dimensionLength", "dimension_breadth": "dimensionBreadth", "dimension_height": "dimensionHeight", "measure_unit_code": "measureUnitCode"}
    payload = {backend: args[name] for name, backend in mapping.items() if args.get(name) is not None}
    payload["billingAddress"] = _create_address_payload(args["billing_address"])
    payload["shippingAddress"] = _create_address_payload(args["shipping_address"])
    payload["orderItems"] = [_create_item_payload(item) for item in args["items"]]
    return payload


def _create_address_payload(address: dict[str, Any]) -> dict[str, Any]:
    mapping = {"name": "name", "phone": "phone", "address_line1": "addressLine1", "city": "city", "state_or_region": "stateOrRegion", "postal_code": "postalCode", "country_code": "countryCode", "company_legal_name": "companyLegalName", "email": "email", "address_line2": "addressLine2", "address_line3": "addressLine3", "county": "county", "district": "district", "municipality": "municipality", "taxing_region": "taxingRegion"}
    return {backend: address[name] for name, backend in mapping.items() if name in address}


def _create_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    mapping = {"name": "name", "quantity_ordered": "quantityOrdered", "item_price": "itemPrice", "code": "code", "channel_sku": "channelSku", "item_image": "itemImage", "product_id": "productId"}
    return {backend: item[name] for name, backend in mapping.items() if name in item}


async def _load_create_order_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_create_order_input(args, context)
    return {"id": f"create-order:{args['order_no'].strip()}", "reference": args["order_no"].strip()}


def _validate_create_order(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_create_order_input(args, context)


def _preview_create_order(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    items = [{"name": item["name"], "quantity_ordered": item["quantity_ordered"], "item_price": item["item_price"]} for item in args["items"]]
    estimated_total = sum(item["quantity_ordered"] * item["item_price"] for item in args["items"])
    return {"summary": f"Create order {target['reference']}", "proposed_changes": {"order_no": target["reference"], "seller_id": args["seller_id"], "channel_id": args["channel_id"], "customer_id": args.get("customer_id"), "items": items, "estimated_total": estimated_total}, "side_effects": ["Creates the order and address records. Address validation may create validation records or an Address Issue tag."], "warnings": ["Payment is not processed, stock is not reserved, and paymentMethod is informational only."]}


async def _execute_create_order(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    return await get_api().async_post("/commerce/orders", _create_order_payload(args), retry=False)


CREATE_ORDER_SPEC = WriteToolSpec(
    name="create_order",
    description="Preview and, after explicit confirmation, create a PointNXT order. Payment is not processed, stock is not reserved, and tenant identity is derived from the authenticated session.",
    input_schema={"type": "object", "properties": {"order_no": {"type": "string", "description": "Non-empty order number."}, "order_date": {"type": "string", "description": "ISO order date."}, "seller_id": {"type": "string", "description": "Seller UUID."}, "channel_id": {"type": "string", "description": "Channel UUID."}, "billing_address": {"type": "object", "description": "Required billing address."}, "shipping_address": {"type": "object", "description": "Required shipping address."}, "items": {"type": "array", "description": "Non-empty order item array."}, "customer_id": {"type": "string", "description": "Optional customer UUID."}, "currency": {"type": "string", "description": "Optional ISO 4217 currency."}, "payment_method": {"type": "string", "description": "Informational payment method; payment is not processed."}, "fulfillment_instruction": {"type": "string", "description": "Optional fulfillment instruction."}, "order_weight": {"type": "number", "description": "Optional positive finite weight."}, "weight_unit_code": {"type": "string", "enum": sorted(_ORDER_WEIGHT_UNITS), "description": "Weight unit."}, "dimension_length": {"type": "number", "description": "Positive finite length."}, "dimension_breadth": {"type": "number", "description": "Positive finite breadth."}, "dimension_height": {"type": "number", "description": "Positive finite height."}, "measure_unit_code": {"type": "string", "enum": sorted(_ORDER_MEASURE_UNITS), "description": "Measure unit."}}, "required": ["order_no", "order_date", "seller_id", "channel_id", "billing_address", "shipping_address", "items"]},
    destructive=True, open_world=False, load_target=_load_create_order_target, validate=_validate_create_order, build_preview=_preview_create_order, execute=_execute_create_order,
)


def get_api() -> PointNXTAPI:
    return PointNXTAPI()


SUPPORTED_ORDER_STATUSES = frozenset(ORDER_STATUSES)
MAX_ORDER_PAGE_SIZE = 100


async def _load_order_note_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_order_note_input(args)
    response = await get_api().async_get(f"/commerce/orders/{args['order_id'].strip()}")
    order = response.get("data", response) if isinstance(response, dict) else {}
    if not isinstance(order, dict) or not order.get("id"):
        raise WriteError("NOT_FOUND", "The order was not found.")
    return {
        "id": str(order["id"]),
        "reference": order.get("orderNo") or str(order["id"]),
        "version": order.get("version") or order.get("updatedAt") or order.get("updated_at"),
    }


def _validate_order_note_input(args: dict[str, Any]) -> None:
    order_id, note = args.get("order_id"), args.get("note")
    if not isinstance(order_id, str):
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.")
    try:
        UUID(order_id.strip())
    except ValueError:
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.") from None
    if not isinstance(note, str) or not note.strip():
        raise WriteError("VALIDATION_ERROR", "note must be a non-empty string.")
    if len(note.strip()) > 2000:
        raise WriteError("VALIDATION_ERROR", "note must be at most 2000 characters.")


def _validate_order_note(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_order_note_input(args)


def _preview_order_note(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    return {
        "summary": f"Add an internal note to order {target['reference']}",
        "proposed_changes": {"order_id": target["id"], "note": args["note"].strip()},
        "side_effects": ["Creates one internal order note in PointNXT."],
    }


async def _execute_order_note(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    response = await get_api().async_post("/commerce/order-note", {"orderId": target["id"], "note": args["note"].strip()}, retry=False)
    data = response.get("data", response) if isinstance(response, dict) else {}
    result = {"success": True, "orderId": target["id"], "message": "Order note created successfully."}
    if isinstance(data, dict) and data.get("id"):
        result["noteId"] = data["id"]
    return result


async def _load_order_tags_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_order_tags_input(args)
    response = await get_api().async_get(f"/commerce/orders/{args['order_id'].strip()}")
    order = response.get("data", response) if isinstance(response, dict) else {}
    if not isinstance(order, dict) or not order.get("id"):
        raise WriteError("NOT_FOUND", "The order was not found.")
    return {
        "id": str(order["id"]),
        "reference": order.get("orderNo") or str(order["id"]),
        "version": order.get("version") or order.get("updatedAt") or order.get("updated_at"),
    }


def _validate_order_tags_input(args: dict[str, Any]) -> None:
    order_id = args.get("order_id")
    if not isinstance(order_id, str):
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.")
    try:
        UUID(order_id.strip())
    except ValueError:
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.") from None

    tags = args.get("tags")
    if not isinstance(tags, list):
        raise WriteError("VALIDATION_ERROR", "tags must be an array of strings.")
    if any(not isinstance(tag, str) for tag in tags):
        raise WriteError("VALIDATION_ERROR", "every tag must be a string.")


def _validate_order_tags(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_order_tags_input(args)


def _preview_order_tags(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    return {
        "summary": f"Replace all tags on order {target['reference']}",
        "proposed_changes": {"order_id": target["id"], "tags": args["tags"]},
        "side_effects": ["Replaces the complete existing order tag list in PointNXT."],
        "warnings": ["This operation replaces ALL existing tags; it does not append tags."],
    }


async def _execute_order_tags(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    return await get_api().async_request(
        "PUT",
        f"/commerce/orders/{target['id']}/tags",
        json={"tags": args["tags"]},
        retry=False,
    )


UPDATE_ORDER_TAGS_SPEC = WriteToolSpec(
    name="update_order_tags",
    description=(
        "Preview and, after explicit confirmation, replace the complete set of tags "
        "on an existing PointNXT order. This replaces all existing tags; it does not append tags."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Internal PointNXT order UUID."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Complete replacement list of order tag names. Every item must be a string.",
            },
        },
        "required": ["order_id", "tags"],
    },
    destructive=True,
    open_world=False,
    load_target=_load_order_tags_target,
    validate=_validate_order_tags,
    build_preview=_preview_order_tags,
    execute=_execute_order_tags,
)


async def _load_change_order_status_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_change_order_status_input(args)
    api = get_api()
    order_response, transitions_response = await asyncio.gather(
        api.async_get(f"/commerce/orders/{args['order_id'].strip()}"),
        api.async_get("/commerce/orders/status-transitions"),
    )
    order = order_response.get("data", order_response) if isinstance(order_response, dict) else {}
    if not isinstance(order, dict) or not order.get("id"):
        raise WriteError("NOT_FOUND", "The order was not found.")
    current_status = order.get("orderStatus")
    if not isinstance(current_status, str) or not current_status.strip():
        raise WriteError("VALIDATION_ERROR", "The order does not have a valid orderStatus.")
    current_status = current_status.strip().upper()
    transitions = parse_permitted_transitions(transitions_response)
    permitted = transitions.get(current_status)
    requested_status = args["order_status"].strip().upper()
    if not permitted or requested_status not in permitted:
        allowed = sorted(permitted or set())
        raise WriteError(
            "TRANSITION_NOT_PERMITTED",
            f"Transition from {current_status} to {requested_status} is not permitted by PointNXT.",
            details={"current_status": current_status, "requested_status": requested_status, "permitted": allowed},
        )
    return {
        "id": str(order["id"]),
        "reference": order.get("orderNo") or str(order["id"]),
        "version": order.get("version") or order.get("updatedAt") or order.get("updated_at"),
        "current_status": current_status,
    }


def _validate_change_order_status_input(args: dict[str, Any]) -> None:
    order_id = args.get("order_id")
    if not isinstance(order_id, str):
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.")
    try:
        UUID(order_id.strip())
    except ValueError:
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.") from None

    order_status = args.get("order_status")
    if not isinstance(order_status, str) or not order_status.strip():
        raise WriteError("VALIDATION_ERROR", "order_status must be a non-empty string.")
    if order_status.strip().upper() not in SUPPORTED_ORDER_STATUSES:
        raise WriteError("VALIDATION_ERROR", "order_status is not a valid PointNXT order status.")

    version = args.get("version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
        raise WriteError("VALIDATION_ERROR", "version must be a non-negative integer.")
    for name in ("notes", "cancel_reason"):
        value = args.get(name)
        if value is not None and not isinstance(value, str):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a string when provided.")


def _validate_change_order_status(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_change_order_status_input(args)


def _preview_change_order_status(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    return {
        "summary": f"Change order {target['reference']} from {target['current_status']} to {args['order_status'].strip().upper()}",
        "proposed_changes": {
            "order_id": target["id"],
            "order_status": args["order_status"].strip().upper(),
            **{name: args[name] for name in ("notes", "version", "cancel_reason") if args.get(name) is not None},
        },
        "side_effects": ["Updates the order status through PointNXT's backend state machine."],
        "warnings": ["PointNXT will enforce transition, channel, fulfillment, and version business rules."],
    }


async def _execute_change_order_status(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    payload = {"orderStatus": args["order_status"].strip().upper()}
    for arg_name, backend_name in (("notes", "notes"), ("version", "version"), ("cancel_reason", "cancelReason")):
        if args.get(arg_name) is not None:
            payload[backend_name] = args[arg_name]
    return await get_api().async_request(
        "PUT",
        f"/commerce/orders/{target['id']}",
        json=payload,
        retry=False,
    )


CHANGE_ORDER_STATUS_SPEC = WriteToolSpec(
    name="change_order_status",
    description=(
        "Preview and, after explicit confirmation, change an order's status through the "
        "PointNXT backend state machine. Allowed transitions are fetched from PointNXT; "
        "backend validation is never bypassed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Internal PointNXT order UUID."},
            "order_status": {"type": "string", "description": "Target PointNXT order status."},
            "notes": {"type": "string", "description": "Optional status-change notes."},
            "version": {"type": "integer", "description": "Optional order version for optimistic concurrency."},
            "cancel_reason": {"type": "string", "description": "Optional cancellation reason."},
        },
        "required": ["order_id", "order_status"],
    },
    destructive=True,
    open_world=False,
    load_target=_load_change_order_status_target,
    validate=_validate_change_order_status,
    build_preview=_preview_change_order_status,
    execute=_execute_change_order_status,
)


_WEIGHT_UNITS = frozenset({"GM", "KG", "LB", "OZ"})
_MEASURE_UNITS = frozenset({"MM", "CM", "IN"})


async def _load_update_order_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_update_order_input(args)
    response = await get_api().async_get(f"/commerce/orders/{args['order_id'].strip()}")
    order = response.get("data", response) if isinstance(response, dict) else {}
    if not isinstance(order, dict) or not order.get("id"):
        raise WriteError("NOT_FOUND", "The order was not found.")
    version = order.get("version")
    if version is None:
        raise WriteError("VALIDATION_ERROR", "The order response did not contain a version.")
    return {"id": str(order["id"]), "reference": order.get("orderNo") or str(order["id"]), "version": version}


def _validate_update_order_input(args: dict[str, Any]) -> None:
    order_id = args.get("order_id")
    if not isinstance(order_id, str):
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.")
    try:
        UUID(order_id.strip())
    except ValueError:
        raise WriteError("VALIDATION_ERROR", "order_id must be a valid UUID.") from None

    actual_fields = ("fulfillment_instruction", "order_weight", "dimension_length", "dimension_breadth", "dimension_height")
    if not any(args.get(name) is not None for name in actual_fields):
        raise WriteError("VALIDATION_ERROR", "At least one order update field is required.")
    if args.get("fulfillment_instruction") is not None and not isinstance(args["fulfillment_instruction"], str):
        raise WriteError("VALIDATION_ERROR", "fulfillment_instruction must be a string.")
    for name in ("order_weight", "dimension_length", "dimension_breadth", "dimension_height"):
        value = args.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a positive finite number.")
    for name, allowed in (("weight_unit_code", _WEIGHT_UNITS), ("measure_unit_code", _MEASURE_UNITS)):
        value = args.get(name)
        if value is not None and (not isinstance(value, str) or value.upper() not in allowed):
            raise WriteError("VALIDATION_ERROR", f"{name} must be one of: {', '.join(sorted(allowed))}.")


def _validate_update_order(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_update_order_input(args)


def _preview_update_order(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    fields = ("fulfillment_instruction", "order_weight", "weight_unit_code", "dimension_length", "dimension_breadth", "dimension_height", "measure_unit_code")
    return {
        "summary": f"Update order {target['reference']} fulfillment and/or measurements",
        "proposed_changes": {"order_id": target["id"], **{name: args[name] for name in fields if args.get(name) is not None}},
        "current_version": target["version"],
        "side_effects": ["Updates the selected order fields through PointNXT."],
    }


async def _execute_update_order(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    field_map = {
        "fulfillment_instruction": "fulfillmentInstruction", "order_weight": "orderWeight", "weight_unit_code": "weightUnitCode",
        "dimension_length": "dimensionLength", "dimension_breadth": "dimensionBreadth", "dimension_height": "dimensionHeight",
        "measure_unit_code": "measureUnitCode",
    }
    payload = {backend: args[name] for name, backend in field_map.items() if args.get(name) is not None}
    version = target["version"]
    payload["version"] = int(version) if isinstance(version, str) and version.isdigit() else version
    return await get_api().async_request("PUT", f"/commerce/orders/{target['id']}", json=payload, retry=False)


UPDATE_ORDER_SPEC = WriteToolSpec(
    name="update_order",
    description="Preview and, after explicit confirmation, update approved fulfillment and measurement fields on a PointNXT order. Status, notes, addresses, totals, currency, force, cancelReason, warehouseId, and model-provided version are not accepted.",
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Internal PointNXT order UUID."},
            "fulfillment_instruction": {"type": "string", "description": "Optional fulfillment instruction."},
            "order_weight": {"type": "number", "description": "Optional positive finite order weight."},
            "weight_unit_code": {"type": "string", "enum": sorted(_WEIGHT_UNITS), "description": "Optional weight unit: GM, KG, LB, or OZ."},
            "dimension_length": {"type": "number", "description": "Optional positive finite length."},
            "dimension_breadth": {"type": "number", "description": "Optional positive finite breadth."},
            "dimension_height": {"type": "number", "description": "Optional positive finite height."},
            "measure_unit_code": {"type": "string", "enum": sorted(_MEASURE_UNITS), "description": "Optional measure unit: MM, CM, or IN."},
        },
        "required": ["order_id"],
    },
    destructive=True,
    open_world=False,
    load_target=_load_update_order_target,
    validate=_validate_update_order,
    build_preview=_preview_update_order,
    execute=_execute_update_order,
    load_target_on_commit=False,
)


ADD_ORDER_NOTE_SPEC = WriteToolSpec(
    name="add_order_note",
    description="Preview and, after explicit confirmation, add an internal note to an existing PointNXT order. Use the internal order UUID.",
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Internal PointNXT order UUID."},
            "note": {
                "type": "string",
                "format": "free-text",
                "maxLength": 2000,
                "description": "Internal order note, maximum 2000 characters.",
            },
        },
        "required": ["order_id", "note"],
    },
    destructive=False, open_world=False, load_target=_load_order_note_target, validate=_validate_order_note, build_preview=_preview_order_note, execute=_execute_order_note,
)


def _validate_order_filters(
    limit: int,
    page: int,
    order_status: str | None,
    start_date: str | None,
    end_date: str | None,
    timezone_name: str | None,
) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if limit > MAX_ORDER_PAGE_SIZE:
        raise ValueError(f"limit must be at most {MAX_ORDER_PAGE_SIZE}")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise ValueError("page must be a positive integer")

    if order_status is not None:
        if not isinstance(order_status, str):
            raise ValueError("order_status must be a string")
        normalized_status = order_status.upper()
        if normalized_status not in SUPPORTED_ORDER_STATUSES:
            raise ValueError(
                f"Invalid order_status {order_status!r}. Valid values: "
                f"{', '.join(sorted(SUPPORTED_ORDER_STATUSES))}"
            )

    local_date_period(start_date, end_date, timezone_name)


async def get_orders(
    limit: int = 10,
    page: int = 1,
    order_status: str | None = None,
    payment_method: str | None = None,
    channel_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    order_no: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    channel_order_id: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    _validate_order_filters(limit, page, order_status, start_date, end_date, timezone)
    period = local_date_period(start_date, end_date, timezone)
    timezone_name, _ = resolve_timezone(timezone)

    params = {}
    search = order_no
    if not search and customer_email:
        search = normalize_email(customer_email)
    elif not search and customer_phone:
        search = normalize_phone(customer_phone)
    optional_params = {
        "limit": limit,
        "page": page,
        "orderStatus": order_status.upper() if order_status else None,
        "paymentMethod": payment_method,
        "channelId": channel_id,
        "sellerId": seller_id,
        "warehouseId": warehouse_id,
        "customerId": customer_id,
        "dateFrom": period["utc_start"],
        "dateTo": period["utc_end"],
        "search": (search or channel_order_id),
    }

    for name, value in optional_params.items():
        if value is not None:
            params[name] = value

    response = await get_api().async_get("/commerce/orders", params=params)
    return {**response, "timezone": timezone_name, "period": period}


async def get_orders_summary(
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    orders_response = await get_orders(
        limit=limit if limit is not None else 10,
        page=page if page is not None else 1,
        order_status=order_status,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
    )
    data = orders_response.get("data", {})
    orders = [
        order for order in (data.get("items", []) or []) if not order.get("isTest")
    ]

    def order_amount(order: dict[str, Any]) -> float:
        return order.get("grandTotal", order.get("orderTotal", 0)) or 0

    def money(value: Any) -> float | int:
        if value is None:
            return 0
        return round(value, 2)

    total_order_value = sum(order_amount(order) for order in orders)
    status_breakdown = {}
    payment_method_breakdown = {}

    for order in orders:
        status = order.get("orderStatus") or "UNKNOWN"
        payment_method = order.get("paymentMethod") or "UNKNOWN"
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        payment_method_breakdown[payment_method] = (
            payment_method_breakdown.get(payment_method, 0) + 1
        )

    recent_orders = sorted(
        orders,
        key=lambda order: order.get("orderDate") or order.get("createdAt") or "",
        reverse=True,
    )[:5]

    return {
        "found": bool(orders),
        "period": {
            "start_date": start_date,
            "end_date": end_date,
            **local_date_period(start_date, end_date, timezone),
        },
        "timezone": resolve_timezone(timezone)[0],
        "pagination": {
            "page": page if page is not None else 1,
            "limit": limit if limit is not None else 10,
            "in_page": len(orders),
            "total": data.get("totalItems", data.get("total", len(orders))),
        },
        "total_orders": len(orders),
        "total_order_value": money(total_order_value),
        "average_order_value": money(total_order_value / len(orders) if orders else 0),
        "status_breakdown": status_breakdown,
        "payment_method_breakdown": payment_method_breakdown,
        "top_5_recent_orders": [
            {
                "order_number": order.get("orderNo"),
                "customer": order.get("customer"),
                "amount": money(order_amount(order)),
                "status": order.get("orderStatus"),
            }
            for order in recent_orders
        ],
    }


async def search_orders(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    payment_method: str | None = None,
    channel_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    channel_order_id: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Search orders using the existing PointNXT order-list endpoint.

    PointNXT exposes one combined order search parameter, so customer_name is
    passed through the same backend search field as the other text searches.
    """
    if customer_email or customer_phone:
        from tools.customers import orders_for_contact

        result = await orders_for_contact(
            customer_email,
            customer_phone,
            limit,
            page,
            order_status=order_status,
            payment_method=payment_method,
            channel_id=channel_id,
            seller_id=seller_id,
            warehouse_id=warehouse_id,
            start_date=start_date,
            end_date=end_date,
            channel_order_id=channel_order_id,
            timezone=timezone,
        )
    else:
        result = await get_orders(
            limit=limit,
            page=page,
            order_status=order_status,
            payment_method=payment_method,
            channel_id=channel_id,
            seller_id=seller_id,
            warehouse_id=warehouse_id,
            customer_id=customer_id,
            start_date=start_date,
            end_date=end_date,
            order_no=order_reference or customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            channel_order_id=channel_order_id,
            timezone=timezone,
        )
    data = result.get("data", {})
    items = data.get("items", []) or []
    return {
        "found": bool(items),
        "message": None if items else "No matching orders found.",
        "timezone": resolve_timezone(timezone)[0],
        "period": local_date_period(start_date, end_date, timezone),
        "pagination": data.get("metadata", {}),
        **{
            key: result[key]
            for key in ("results_are_partial", "note")
            if key in result
        },
        "orders": [
            {
                "id": order.get("id"),
                "order_number": order.get("orderNo"),
                "channel_order_number": order.get("channelOrderNo"),
                "order_date": order.get("orderDate"),
                "order_date_local": local_timestamp(order.get("orderDate"), timezone),
                "status": order.get("orderStatus"),
                "customer": order.get("customer"),
                "total": order.get("grandTotal", order.get("orderTotal")),
                "currency": order.get("currency"),
            }
            for order in items
        ],
    }


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _non_zero(value: Any) -> bool:
    return value is not None and value != 0


def _compact_order(order: dict[str, Any], timezone_name: str | None = None) -> dict[str, Any]:
    """Shape one order into the support-focused response used by ``get_order``."""
    customer = order.get("customer") or {}
    address = order.get("shippingAddress") or order.get("billingAddress") or {}
    first_name = customer.get("firstName") or address.get("firstName")
    last_name = customer.get("lastName") or address.get("lastName")
    customer_name = customer.get("name") or " ".join(
        part for part in (first_name, last_name) if part
    )

    compact_items = []
    for item in order.get("orderItems", []) or []:
        compact_items.append(
            _without_none({
                "name": item.get("name"),
                "sku": item.get("sku") or item.get("code") or item.get("channelSku"),
                "quantity_ordered": item.get("quantityOrdered"),
                "quantity_shipped": item.get("quantityShipped"),
                "quantity_returned": item.get("quantityReturned"),
                "item_status": item.get("itemStatus") or item.get("status"),
                "unit_price": item.get("unitPrice") or item.get("price"),
                "line_total": item.get("lineTotal") or item.get("totalPrice"),
                **{
                    key: value
                    for key, value in {
                        "tax": item.get("taxAmount", item.get("tax")),
                        "discount": item.get("discountAmount", item.get("discount")),
                        "shipping": item.get("shippingAmount", item.get("shipping")),
                        "cod_fee": item.get("codFee", item.get("codAmount")),
                        "gift_wrap": item.get("giftWrapAmount", item.get("giftWrapFee")),
                    }.items()
                    if _non_zero(value)
                },
            })
        )

    compact_shipments = []
    for shipment in order.get("shipments", []) or []:
        shipment_values = {
            "awb_no": shipment.get("awbNo") or shipment.get("awb"),
            "ship_status": shipment.get("shipStatus"),
            "piece_count": shipment.get("pieceCount"),
            "weight_grams": shipment.get("weightGrams"),
            "cod_collectable_amount": shipment.get("codCollectableAmount"),
            "is_return": shipment.get("isReturn"),
            "created_at": shipment.get("createdAt"),
            # A null delivered_at is meaningful: it means not delivered yet.
            "delivered_at": shipment.get("deliveredAt"),
        }
        compact_shipments.append(
            {
                key: value
                for key, value in shipment_values.items()
                if value is not None or key == "delivered_at"
            }
        )

    warehouse = order.get("warehouse") or {}
    status = order.get("orderStatus")
    order_date = order.get("orderDate") or order.get("createdAt")
    confirmed_at = order.get("confirmedAt")
    fulfilled_at = order.get("fulfilledAt")
    if not confirmed_at and status in {
        "CONFIRMED",
        "PARTIALLY_FULFILLED",
        "FULFILLED",
        "DELIVERED",
    }:
        confirmed_at = order_date
    if not fulfilled_at and status in {"FULFILLED", "DELIVERED"}:
        fulfilled_at = order_date
    compact = {
        "id": order.get("id"),
        "order_number": order.get("orderNo"),
        "channel_order_number": order.get("channelOrderNo"),
        "order_date": order.get("orderDate"),
        "order_date_local": local_timestamp(order.get("orderDate"), timezone_name),
        "status": status,
        "confirmed_at": confirmed_at,
        "fulfilled_at": fulfilled_at,
        "customer": _without_none({
            "id": customer.get("id") or order.get("customerId"),
            "name": customer_name or None,
            "email": customer.get("email") or address.get("email"),
            "phone": customer.get("phone") or address.get("phone"),
            "state": customer.get("state") or address.get("state"),
        }),
        "items": compact_items,
        "payment": _without_none({
            "method": order.get("paymentMethod"),
            "total": order.get("grandTotal", order.get("orderTotal")),
            "paid": order.get("paidAmount"),
            "refunded": order.get("refundAmount"),
            "currency": order.get("currency"),
        }),
        "fulfillment": _without_none({
            "warehouse": _without_none({
                "id": warehouse.get("id"),
                "code": warehouse.get("code"),
                "name": warehouse.get("name"),
            }),
            "address_validation_status": order.get("addressValidationStatus"),
        }),
        "shipments": compact_shipments,
        "returns": _without_none({
            "returned_at": order.get("returnedAt"),
            "return_reason": order.get("returnReason"),
        }),
    }
    return _without_none(compact)


async def get_order(order_reference: str) -> dict[str, Any]:
    """Retrieve compact support-focused details by order ID or display/reference number."""
    if not isinstance(order_reference, str) or not order_reference.strip():
        raise ValueError("order_reference must be a non-empty string")

    reference = order_reference.strip()
    reference = reference.removeprefix("#")
    search = await get_orders(limit=1, page=1, order_no=reference)
    items = search.get("data", {}).get("items", []) or []
    if not items:
        return {"found": False, "order": None, "message": "No matching order found."}
    detail = await get_order_by_id(items[0]["id"])
    order = detail.get("data", detail)
    return {"found": True, "order": _compact_order(order)}


async def get_order_by_id(order_id: str) -> dict[str, Any]:
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be a non-empty string")

    return await get_api().async_get(f"/commerce/orders/{order_id}")


async def get_order_stats() -> dict[str, Any]:
    return await get_api().async_get("/commerce/orders/stats")


async def get_status_transitions() -> dict[str, Any]:
    return await get_api().async_get("/commerce/orders/status-transitions")


def _parse_order_timestamp(order: dict[str, Any]) -> datetime | None:
    for key in ("updatedAt", "updated_at", "orderDate", "createdAt", "created_at"):
        val = order.get(key)
        if not val or not isinstance(val, str):
            continue
        try:
            cleaned = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


async def get_order_details(
    order_id: str | None = None,
    order_reference: str | None = None,
) -> dict[str, Any]:
    """Retrieve complete, sanitized details for a specific order.

    Accepts order_id directly, or resolves an order_reference (order number or
    channel order number) to retrieve complete details from the backend.
    """
    if not order_id and not order_reference:
        raise ValueError("Either order_id or order_reference must be provided")

    resolved_id: str | None = None
    if order_id and isinstance(order_id, str) and order_id.strip():
        resolved_id = order_id.strip()
    elif order_reference and isinstance(order_reference, str) and order_reference.strip():
        reference = order_reference.strip().removeprefix("#")
        search = await get_orders(limit=1, page=1, order_no=reference)
        items = search.get("data", {}).get("items", []) or []
        if items and items[0].get("id"):
            resolved_id = str(items[0]["id"])
        else:
            resolved_id = reference
    else:
        raise ValueError("Invalid order_id or order_reference")

    try:
        raw_detail = await get_order_by_id(resolved_id)
    except Exception as error:
        status = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        if status == 404:
            return {
                "found": False,
                "order_id": resolved_id,
                "order": None,
                "message": f"Order {resolved_id!r} not found.",
            }
        raise

    order = raw_detail.get("data", raw_detail) if isinstance(raw_detail, dict) else {}
    if not order or not isinstance(order, dict) or not order.get("id"):
        return {
            "found": False,
            "order_id": resolved_id,
            "order": None,
            "message": f"Order {resolved_id!r} not found.",
        }

    sanitized = sanitize_public_response(order)
    return {
        "found": True,
        "order_id": order.get("id"),
        "order_number": order.get("orderNo"),
        "order": sanitized,
        "message": None,
    }


async def find_stuck_orders(
    order_status: str = "PENDING",
    threshold_hours: float = 24,
    limit: int = 20,
    page: int = 1,
    warehouse_id: str | None = None,
    channel_id: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Identify orders that have remained in a specified status beyond a configurable time threshold.

    Uses real timestamps to clearly distinguish overdue orders from orders that are on track.
    """
    if isinstance(threshold_hours, bool) or not isinstance(threshold_hours, (int, float)) or threshold_hours <= 0:
        raise ValueError("threshold_hours must be a positive number")

    if not isinstance(order_status, str) or not order_status.strip():
        raise ValueError("order_status must be a non-empty string")

    norm_status = order_status.strip().upper()
    if norm_status not in SUPPORTED_ORDER_STATUSES:
        raise ValueError(
            f"Invalid order_status {order_status!r}. Valid values: "
            f"{', '.join(sorted(SUPPORTED_ORDER_STATUSES))}"
        )

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if limit > MAX_ORDER_PAGE_SIZE:
        raise ValueError(f"limit must be at most {MAX_ORDER_PAGE_SIZE}")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise ValueError("page must be a positive integer")

    timezone_name, _ = resolve_timezone(timezone)
    now = datetime.now(dt_timezone.utc)

    response = await get_orders(
        limit=limit,
        page=page,
        order_status=norm_status,
        warehouse_id=warehouse_id,
        channel_id=channel_id,
        timezone=timezone_name,
    )

    data = response.get("data", {})
    items = data.get("items", []) or []

    stuck_orders = []
    on_track_orders = []

    for order in items:
        dt = _parse_order_timestamp(order)
        raw_timestamp = (
            order.get("updatedAt")
            or order.get("orderDate")
            or order.get("createdAt")
        )
        if dt is not None:
            age_seconds = max(0.0, (now - dt).total_seconds())
            age_hours = round(age_seconds / 3600, 2)
        else:
            age_hours = 0.0

        is_overdue = age_hours >= threshold_hours
        customer = order.get("customer") or {}
        customer_summary = None
        if isinstance(customer, dict) and customer:
            customer_summary = {
                "id": customer.get("id"),
                "name": customer.get("name") or " ".join(
                    part for part in (customer.get("firstName"), customer.get("lastName")) if part
                ) or None,
                "email": customer.get("email"),
                "phone": customer.get("phone"),
            }

        item_entry = _without_none({
            "id": order.get("id"),
            "order_number": order.get("orderNo"),
            "channel_order_number": order.get("channelOrderNo"),
            "status": order.get("orderStatus"),
            "status_timestamp": raw_timestamp,
            "status_timestamp_local": local_timestamp(raw_timestamp, timezone_name),
            "age_hours": age_hours,
            "threshold_hours": threshold_hours,
            "is_overdue": is_overdue,
            "category": "OVERDUE" if is_overdue else "PENDING_ON_TRACK",
            "customer": _without_none(customer_summary) if customer_summary else None,
            "total": order.get("grandTotal", order.get("orderTotal")),
            "currency": order.get("currency"),
            "note": (
                f"Overdue: in {norm_status} for {age_hours:.1f}h (exceeds {threshold_hours}h threshold)"
                if is_overdue
                else f"On track: in {norm_status} for {age_hours:.1f}h (within {threshold_hours}h threshold)"
            ),
        })

        if is_overdue:
            stuck_orders.append(item_entry)
        else:
            on_track_orders.append(item_entry)

    stuck_orders.sort(key=lambda o: o.get("age_hours", 0), reverse=True)

    result = {
        "found": bool(stuck_orders),
        "target_status": norm_status,
        "threshold_hours": threshold_hours,
        "total_evaluated": len(items),
        "stuck_count": len(stuck_orders),
        "on_track_count": len(on_track_orders),
        "timezone": timezone_name,
        "stuck_orders": stuck_orders,
        "on_track_orders": on_track_orders,
        "pagination": data.get("metadata", {}),
        "message": (
            f"Found {len(stuck_orders)} stuck order(s) in {norm_status} exceeding {threshold_hours}h threshold "
            f"({len(on_track_orders)} order(s) on track within threshold)."
            if stuck_orders
            else f"No stuck orders found in {norm_status} exceeding {threshold_hours}h threshold "
            f"({len(on_track_orders)} order(s) inspected are within threshold)."
        ),
    }
    return sanitize_public_response(result)


def parse_permitted_transitions(backend_response: dict[str, Any]) -> dict[str, set[str]]:
    """Parse backend status-transitions payload into a normalized dict[from_status, set[to_statuses]]."""
    data = backend_response.get("data", backend_response)
    transitions: dict[str, set[str]] = {}

    if isinstance(data, dict):
        for from_status, targets in data.items():
            if isinstance(from_status, str):
                from_norm = from_status.upper()
                if isinstance(targets, list):
                    transitions.setdefault(from_norm, set()).update(
                        str(t).upper() for t in targets if isinstance(t, (str, dict))
                    )
                elif isinstance(targets, str):
                    transitions.setdefault(from_norm, set()).add(targets.upper())
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                from_val = (
                    item.get("from")
                    or item.get("fromStatus")
                    or item.get("source")
                    or item.get("status")
                )
                to_val = (
                    item.get("to")
                    or item.get("toStatus")
                    or item.get("target")
                    or item.get("allowed")
                    or item.get("transitions")
                )
                if from_val and isinstance(from_val, str):
                    from_norm = from_val.upper()
                    if isinstance(to_val, list):
                        for t in to_val:
                            if isinstance(t, str):
                                transitions.setdefault(from_norm, set()).add(t.upper())
                            elif isinstance(t, dict) and "to" in t:
                                transitions.setdefault(from_norm, set()).add(str(t["to"]).upper())
                    elif isinstance(to_val, str):
                        transitions.setdefault(from_norm, set()).add(to_val.upper())

    return transitions


async def get_order_status_transitions(
    current_status: str | None = None,
) -> dict[str, Any]:
    """Return valid status transitions supported by the existing PointNXT backend."""
    if current_status is not None:
        if not isinstance(current_status, str):
            raise ValueError("current_status must be a string")
        norm_status = current_status.strip().upper()
        if norm_status not in SUPPORTED_ORDER_STATUSES:
            raise ValueError(
                f"Invalid current_status {current_status!r}. Valid values: "
                f"{', '.join(sorted(SUPPORTED_ORDER_STATUSES))}"
            )
        current_status = norm_status

    raw_response = await get_status_transitions()
    permitted_map = parse_permitted_transitions(raw_response)
    backend_data = raw_response.get("data", raw_response)

    result: dict[str, Any] = {
        "backend_response": backend_data,
        "supported_order_statuses": sorted(SUPPORTED_ORDER_STATUSES),
    }

    if current_status:
        result["current_status"] = current_status
        result["permitted_next_statuses"] = sorted(permitted_map.get(current_status, set()))
    else:
        result["permitted_transitions_by_status"] = {
            status: sorted(targets) for status, targets in sorted(permitted_map.items())
        }

    return sanitize_public_response(result)


_DEFAULT_FALLBACK_SECRET = "pointnxt-default-write-confirmation-secret-32b"
_order_status_state_store = InMemoryWriteStateStore()


def _get_token_manager() -> ConfirmationTokenManager:
    from config import PNXT_CONFIRMATION_SECRET, PNXT_CONFIRMATION_TTL_SECONDS
    secret = (
        PNXT_CONFIRMATION_SECRET
        if PNXT_CONFIRMATION_SECRET and len(PNXT_CONFIRMATION_SECRET.encode()) >= 32
        else _DEFAULT_FALLBACK_SECRET
    )
    return ConfirmationTokenManager(secret, PNXT_CONFIRMATION_TTL_SECONDS)


async def confirm_order_status_change(
    new_status: str,
    order_id: str | None = None,
    order_reference: str | None = None,
    reason: str | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Update an order's status only after validating the requested transition against permitted transitions."""
    if not new_status or not isinstance(new_status, str):
        raise ValueError("new_status is required and must be a string")
    norm_new_status = new_status.strip().upper()
    if norm_new_status not in SUPPORTED_ORDER_STATUSES:
        raise ValueError(
            f"Invalid target status {new_status!r}. Valid values: "
            f"{', '.join(sorted(SUPPORTED_ORDER_STATUSES))}"
        )

    if not order_id and not order_reference:
        raise ValueError("Either order_id or order_reference must be provided")

    # 1. User authentication & authorization verification
    session = get_session()
    is_auth = session and session.is_authenticated()
    from config import DEV_MODE, POINTNXT_ACCESS_TOKEN, POINTNXT_TENANT_ID
    from services.request_context import get_request_key
    dev_auth = DEV_MODE and get_request_key() == "stdio" and bool(POINTNXT_ACCESS_TOKEN and POINTNXT_TENANT_ID)
    if not is_auth and not dev_auth:
        raise WriteError("AUTH_REQUIRED", "Sign in to PointNXT before changing order status.")

    tenant_id = (session.get_tenant_id() if session else None) or POINTNXT_TENANT_ID or "default-tenant"
    user_id = (session.user_id or session.email if session else None) or "dev-user"

    # Sanitize reason
    clean_reason = None
    if reason is not None:
        if not isinstance(reason, str):
            raise ValueError("reason must be a string")
        clean_reason = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", reason).strip()
        if len(clean_reason) > 1000:
            raise ValueError("reason must be at most 1000 characters")

    # Resolve order_id
    resolved_id: str | None = None
    if order_id and isinstance(order_id, str) and order_id.strip():
        resolved_id = order_id.strip()
    elif order_reference and isinstance(order_reference, str) and order_reference.strip():
        reference = order_reference.strip().removeprefix("#")
        search = await get_orders(limit=1, page=1, order_no=reference)
        items = search.get("data", {}).get("items", []) or []
        if items and items[0].get("id"):
            resolved_id = str(items[0]["id"])
        else:
            resolved_id = reference

    if not resolved_id:
        raise ValueError("Could not resolve a valid order ID")

    # 2. Validate order existence
    try:
        raw_detail = await get_order_by_id(resolved_id)
    except Exception as error:
        status_code = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        if status_code == 404:
            raise WriteError("NOT_FOUND", f"Order {resolved_id!r} does not exist.") from error
        raise

    order = raw_detail.get("data", raw_detail) if isinstance(raw_detail, dict) else {}
    if not order or not isinstance(order, dict) or not order.get("id"):
        raise WriteError("NOT_FOUND", f"Order {resolved_id!r} does not exist.")

    current_status = (order.get("orderStatus") or "").upper()
    if not current_status:
        raise WriteError("VALIDATION_ERROR", f"Order {resolved_id} does not have a valid orderStatus.")

    target_version = str(order.get("version") or order.get("updatedAt") or current_status)
    order_no = order.get("orderNo") or resolved_id

    # 3. Permitted status transition validation
    transitions_resp = await get_status_transitions()
    permitted_map = parse_permitted_transitions(transitions_resp)

    # Check permitted transitions if backend provided rules for current_status
    if current_status in permitted_map:
        allowed = permitted_map[current_status]
        if norm_new_status not in allowed and norm_new_status != current_status:
            allowed_str = ", ".join(sorted(allowed)) or "none"
            raise WriteError(
                "TRANSITION_NOT_PERMITTED",
                f"Transition from {current_status} to {norm_new_status} is not permitted by PointNXT backend. "
                f"Permitted next statuses from {current_status}: {allowed_str}.",
                details={"current_status": current_status, "requested_status": norm_new_status, "permitted": sorted(allowed)},
            )

    token_manager = _get_token_manager()
    args_for_hash = {"order_id": resolved_id, "new_status": norm_new_status, "reason": clean_reason}
    args_hash = hashlib.sha256(
        json.dumps(args_for_hash, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    # 4. Preview phase (confirm=False)
    if not confirm:
        fake_spec = type("Spec", (), {"name": "confirm_order_status_change"})()
        fake_context = WriteContext(str(user_id), str(tenant_id))
        target_info = {"id": resolved_id, "version": target_version}
        token, token_payload = token_manager.issue(fake_spec, args_for_hash, target_info, fake_context)

        is_same = current_status == norm_new_status
        warning = f"Order {order_no} is already in status {norm_new_status}." if is_same else None

        preview_result = {
            "status": "preview",
            "action": "confirm_order_status_change",
            "order_id": resolved_id,
            "order_number": order_no,
            "current_status": current_status,
            "requested_status": norm_new_status,
            "valid_transition": True,
            "target_version": target_version,
            "reason": clean_reason,
            "warning": warning,
            "summary": f"Preview: Change status of order {order_no} from {current_status} to {norm_new_status}",
            "confirmation_token": token,
            "expires_at": datetime.fromtimestamp(token_payload["expires_at"], dt_timezone.utc).isoformat(),
            "instructions": "To commit this change, call confirm_order_status_change with confirm=True and confirmation_token.",
        }
        return sanitize_public_response(_without_none(preview_result))

    # 5. Commit phase (confirm=True)
    if not confirmation_token or not isinstance(confirmation_token, str):
        raise WriteError(
            "CONFIRMATION_REQUIRED",
            "Set confirm=true and provide the confirmation_token returned by preview. Run with confirm=false first to preview.",
        )

    # Idempotency check
    if idempotency_key:
        cached = _order_status_state_store.get_idempotency(str(user_id), "confirm_order_status_change", idempotency_key)
        if cached:
            if cached.get("args_hash") != args_hash:
                raise WriteError("IDEMPOTENCY_CONFLICT", "The idempotency key was used with different arguments.")
            return copy.deepcopy(cached["result"])

    # Verify confirmation token
    token_payload = token_manager.verify(confirmation_token)
    if token_payload.get("tool") != "confirm_order_status_change":
        raise WriteError("TOKEN_INVALID", "The confirmation token does not belong to this write tool.")
    if token_payload.get("user_id") != str(user_id) or token_payload.get("tenant_id") != str(tenant_id):
        raise WriteError("TOKEN_INVALID", "The confirmation token does not belong to the current user or tenant.")

    # Single-use nonce check (must precede version check so token replay is detected as TOKEN_USED)
    if not _order_status_state_store.consume_nonce(token_payload["nonce"], token_payload["expires_at"]):
        raise WriteError("TOKEN_USED", "The confirmation token has already been used.")

    if token_payload.get("args_hash") != args_hash:
        raise WriteError("ARGS_CHANGED", "Arguments changed since preview; generate a new preview.")
    if token_payload.get("target_id") != resolved_id:
        raise WriteError("RECORD_CHANGED", "Target order ID changed since preview; generate a new preview.")
    if token_payload.get("target_version") != target_version:
        raise WriteError(
            "RECORD_CHANGED",
            f"The order was modified concurrently since preview (was version {token_payload.get('target_version')}, now {target_version}). Generate a new preview.",
        )

    # Idempotent no-op check if already in target status
    if current_status == norm_new_status:
        audit_id = str(uuid4())
        committed = {
            "status": "committed",
            "order_id": resolved_id,
            "order_number": order_no,
            "previous_status": current_status,
            "new_status": norm_new_status,
            "reason": clean_reason,
            "audit_id": audit_id,
            "idempotent": True,
            "message": f"Order {order_no} is already in status {norm_new_status}. No write needed.",
        }
        sanitized_committed = sanitize_public_response(committed)
        key = idempotency_key or token_payload["nonce"]
        _order_status_state_store.put_idempotency(
            str(user_id), "confirm_order_status_change", key, args_hash, copy.deepcopy(sanitized_committed), time.time() + 86400
        )
        return sanitized_committed

    # Execute backend write
    endpoint = f"/commerce/orders/{resolved_id}/status"
    payload = {
        "orderStatus": norm_new_status,
        "status": norm_new_status,
        "reason": clean_reason,
        "version": order.get("version"),
    }
    started = time.perf_counter()
    api = get_api()
    try:
        write_result = await api.async_request("PUT", endpoint, json=payload, retry=False)
    except Exception as error:
        # Fallback to PUT /commerce/orders/{resolved_id} if /status returns 404/405
        status_code = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        if status_code in (404, 405):
            try:
                write_result = await api.async_request(
                    "PUT", f"/commerce/orders/{resolved_id}", json=payload, retry=False
                )
            except Exception as second_err:
                raise map_backend_error(second_err, str(uuid4())) from second_err
        else:
            raise map_backend_error(error, str(uuid4())) from error

    audit_id = str(uuid4())
    committed = {
        "status": "committed",
        "order_id": resolved_id,
        "order_number": order_no,
        "previous_status": current_status,
        "new_status": norm_new_status,
        "reason": clean_reason,
        "audit_id": audit_id,
        "backend_response": write_result.get("data", write_result) if isinstance(write_result, dict) else write_result,
        "updated_at": datetime.now(dt_timezone.utc).isoformat(),
        "message": f"Successfully updated order {order_no} status from {current_status} to {norm_new_status}.",
    }

    sanitized_committed = sanitize_public_response(committed)
    key = idempotency_key or token_payload["nonce"]
    _order_status_state_store.put_idempotency(
        str(user_id), "confirm_order_status_change", key, args_hash, copy.deepcopy(sanitized_committed), time.time() + 86400
    )

    logger.info(
        "order_status_change_committed",
        extra={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "order_id": resolved_id,
            "previous_status": current_status,
            "new_status": norm_new_status,
            "audit_id": audit_id,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return sanitized_committed

