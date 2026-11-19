import asyncio
import logging
import math
import re
from typing import Any
from uuid import UUID

import httpx

from services.pointnxt_api import PointNXTAPI
from services.write_tools import WriteContext, WriteError, WriteToolSpec
from tools.status_options import PRODUCT_STATUSES

MAX_PRODUCT_PAGE_SIZE = 100
PRODUCT_SKU_SCAN_MAX_PAGES = 25
PRODUCT_SKU_SCAN_PAGE_SIZE = 100
PRODUCT_LIST_COUNT_BASIS = "Paginated products returned by /commerce/products."
PRODUCT_SUMMARY_COUNT_BASIS = (
    "Backend aggregate product summary; its scope may differ from product-list pages."
)
logger = logging.getLogger(__name__)


def get_api() -> PointNXTAPI:
    return PointNXTAPI()


_PRODUCT_WEIGHT_UNITS = frozenset({"GM", "KG", "LB", "OZ"})
_PRODUCT_MEASURE_UNITS = frozenset({"MM", "CM", "IN"})
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _validate_create_product_input(args: dict[str, Any], context: WriteContext) -> None:
    if not context.tenant_id or not str(context.tenant_id).strip():
        raise WriteError("AUTH_REQUIRED", "An authenticated tenant is required to create a product.")
    for name in ("code", "sku", "name"):
        value = args.get(name)
        if not isinstance(value, str) or not value.strip():
            raise WriteError("VALIDATION_ERROR", f"{name} must be a non-empty string.")
    for name in ("barcode", "description"):
        if args.get(name) is not None and not isinstance(args[name], str):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a string.")
    currency = args.get("currency")
    if currency is not None and (not isinstance(currency, str) or not _ISO_CURRENCY.fullmatch(currency.upper())):
        raise WriteError("VALIDATION_ERROR", "currency must be a three-letter ISO 4217 code.")
    for name in ("mrp", "price"):
        value = args.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a finite non-negative number.")
    for name in ("weight", "dimension_length", "dimension_breadth", "dimension_height"):
        value = args.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a positive finite number.")
    for name, allowed in (("weight_unit_code", _PRODUCT_WEIGHT_UNITS), ("measure_unit_code", _PRODUCT_MEASURE_UNITS)):
        value = args.get(name)
        if value is not None and (not isinstance(value, str) or value.upper() not in allowed):
            raise WriteError("VALIDATION_ERROR", f"{name} must be one of: {', '.join(sorted(allowed))}.")
    for name in ("category_id", "vendor_id"):
        value = args.get(name)
        if value is not None:
            try:
                UUID(value.strip()) if isinstance(value, str) else (_ for _ in ()).throw(ValueError())
            except ValueError:
                raise WriteError("VALIDATION_ERROR", f"{name} must be a valid UUID.") from None
    if not any(args.get(name) is not None for name in _PRODUCT_CREATE_FIELDS):
        raise WriteError("VALIDATION_ERROR", "At least one product field is required.")


_PRODUCT_CREATE_FIELDS = (
    "code", "sku", "name", "barcode", "description", "currency", "mrp", "price", "weight",
    "weight_unit_code", "dimension_length", "dimension_breadth", "dimension_height",
    "measure_unit_code", "is_fragile", "category_id", "vendor_id",
)


def _validate_create_product(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    if args.get("is_fragile") is not None and not isinstance(args["is_fragile"], bool):
        raise WriteError("VALIDATION_ERROR", "is_fragile must be a boolean.")
    _validate_create_product_input(args, context)


def _create_product_payload(args: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    field_map = {
        "code": "code", "sku": "sku", "name": "name", "barcode": "barcode", "description": "description",
        "currency": "currency", "mrp": "mrp", "price": "price", "weight": "weight", "weight_unit_code": "weightUnitCode",
        "dimension_length": "dimensionLength", "dimension_breadth": "dimensionBreadth", "dimension_height": "dimensionHeight",
        "measure_unit_code": "measureUnitCode", "is_fragile": "isFragile", "category_id": "categoryId", "vendor_id": "vendorId",
    }
    payload = {backend: args[name] for name, backend in field_map.items() if args.get(name) is not None}
    payload["tenantId"] = tenant_id
    return payload


def _preview_create_product(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    payload = _create_product_payload(args, context.tenant_id)
    payload.pop("tenantId", None)
    return {"summary": f"Create product {args['sku'].strip()}", "proposed_changes": payload, "side_effects": ["Creates one product in PointNXT; no inventory record or channel listings are created."]}


async def _load_create_product_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_create_product_input(args, context)
    return {"id": f"create-product:{args['sku'].strip()}"}


async def _execute_create_product(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    _validate_create_product_input(args, context)
    return await get_api().async_post("/commerce/products", _create_product_payload(args, context.tenant_id), retry=False)


CREATE_PRODUCT_SPEC = WriteToolSpec(
    name="create_product",
    description="Preview and, after explicit confirmation, create a PointNXT product. Tenant identity is taken from the authenticated session and is never a model input.",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Required product code."}, "sku": {"type": "string", "description": "Required product SKU."}, "name": {"type": "string", "description": "Required product name."},
            "barcode": {"type": "string", "description": "Optional barcode."}, "description": {"type": "string", "description": "Optional product description."}, "currency": {"type": "string", "description": "Optional ISO 4217 currency code."},
            "mrp": {"type": "number", "description": "Optional finite non-negative MRP."}, "price": {"type": "number", "description": "Optional finite non-negative price."},
            "weight": {"type": "number", "description": "Optional positive finite weight."}, "weight_unit_code": {"type": "string", "enum": sorted(_PRODUCT_WEIGHT_UNITS), "description": "Optional weight unit."},
            "dimension_length": {"type": "number", "description": "Optional positive finite length."}, "dimension_breadth": {"type": "number", "description": "Optional positive finite breadth."}, "dimension_height": {"type": "number", "description": "Optional positive finite height."}, "measure_unit_code": {"type": "string", "enum": sorted(_PRODUCT_MEASURE_UNITS), "description": "Optional measure unit."},
            "is_fragile": {"type": "boolean", "description": "Optional fragile-product flag."}, "category_id": {"type": "string", "description": "Optional category UUID."}, "vendor_id": {"type": "string", "description": "Optional vendor UUID."},
        },
        "required": ["code", "sku", "name"],
    },
    destructive=True, open_world=False, load_target=_load_create_product_target, validate=_validate_create_product, build_preview=_preview_create_product, execute=_execute_create_product,
)


_PRODUCT_UPDATE_FIELDS = (
    "name", "subtitle", "description", "image", "hscode", "coo", "tax_code", "classification_code",
    "weight", "weight_unit_code", "dimension_length", "dimension_breadth", "dimension_height",
    "measure_unit_code", "is_fragile", "category_id",
)
_PRODUCT_UPDATE_VALUE_FIELDS = (
    "name", "subtitle", "description", "image", "hscode", "coo", "tax_code", "classification_code",
    "weight", "dimension_length", "dimension_breadth", "dimension_height", "is_fragile", "category_id",
)


def _validate_update_product_input(args: dict[str, Any], context: WriteContext) -> None:
    if not context.tenant_id or not str(context.tenant_id).strip():
        raise WriteError("AUTH_REQUIRED", "An authenticated tenant is required to update a product.")
    product_id = args.get("product_id")
    if not isinstance(product_id, str):
        raise WriteError("VALIDATION_ERROR", "product_id must be a valid UUID.")
    try:
        UUID(product_id.strip())
    except ValueError:
        raise WriteError("VALIDATION_ERROR", "product_id must be a valid UUID.") from None
    if not any(args.get(name) is not None for name in _PRODUCT_UPDATE_VALUE_FIELDS):
        raise WriteError("VALIDATION_ERROR", "At least one product update field is required.")
    for name in _PRODUCT_UPDATE_FIELDS:
        if name in args and args[name] is None:
            raise WriteError("VALIDATION_ERROR", f"{name} cannot be null.")
    for name in ("name", "subtitle", "description", "image", "hscode", "coo", "tax_code", "classification_code"):
        if name in args and not isinstance(args[name], str):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a string.")
    for name in ("weight", "dimension_length", "dimension_breadth", "dimension_height"):
        value = args.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a positive finite number.")
    for name, allowed in (("weight_unit_code", _PRODUCT_WEIGHT_UNITS), ("measure_unit_code", _PRODUCT_MEASURE_UNITS)):
        value = args.get(name)
        if value is not None and (not isinstance(value, str) or value.upper() not in allowed):
            raise WriteError("VALIDATION_ERROR", f"{name} must be one of: {', '.join(sorted(allowed))}.")
    if "is_fragile" in args and not isinstance(args["is_fragile"], bool):
        raise WriteError("VALIDATION_ERROR", "is_fragile must be a boolean.")
    if "category_id" in args:
        try:
            UUID(args["category_id"].strip()) if isinstance(args["category_id"], str) else (_ for _ in ()).throw(ValueError())
        except ValueError:
            raise WriteError("VALIDATION_ERROR", "category_id must be a valid UUID.") from None


async def _load_update_product_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_update_product_input(args, context)
    response = await get_api().async_get(f"/commerce/products/{args['product_id'].strip()}")
    product = response.get("data", response) if isinstance(response, dict) else {}
    if not isinstance(product, dict) or not product.get("id"):
        raise WriteError("NOT_FOUND", "The product was not found.")
    product_tenant = product.get("tenantId")
    if not isinstance(product_tenant, str) or not product_tenant:
        raise WriteError("AUTH_REQUIRED", "The product tenant could not be verified.")
    if product_tenant != str(context.tenant_id):
        raise WriteError("NOT_PERMITTED", "The product does not belong to the authenticated tenant.")
    return {"id": str(product["id"]), "reference": product.get("sku") or str(product["id"]), "tenant_verified": True}


def _validate_update_product(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_update_product_input(args, context)


def _update_product_payload(args: dict[str, Any]) -> dict[str, Any]:
    field_map = {
        "name": "name", "subtitle": "subtitle", "description": "description", "image": "image", "hscode": "hscode",
        "coo": "coo", "tax_code": "taxCode", "classification_code": "classificationCode", "weight": "weight",
        "weight_unit_code": "weightUnitCode", "dimension_length": "dimensionLength", "dimension_breadth": "dimensionBreadth",
        "dimension_height": "dimensionHeight", "measure_unit_code": "measureUnitCode", "is_fragile": "isFragile", "category_id": "categoryId",
    }
    return {backend: args[name] for name, backend in field_map.items() if args.get(name) is not None}


def _preview_update_product(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    return {
        "summary": f"Update product {target['reference']}",
        "proposed_changes": {"product_id": target["id"], **_update_product_payload(args)},
        "side_effects": ["Updates only the supplied approved product fields in PointNXT."],
    }


async def _execute_update_product(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    # The framework reloads this target on commit, so tenant ownership is checked again
    # immediately before the PUT without accepting or sending tenantId.
    return await get_api().async_request(
        "PUT",
        f"/commerce/products/{target['id']}",
        json=_update_product_payload(args),
        retry=False,
    )


UPDATE_PRODUCT_SPEC = WriteToolSpec(
    name="update_product",
    description="Preview and, after explicit confirmation, update approved fields on a PointNXT product after verifying it belongs to the authenticated tenant. TenantId and excluded ownership, status, pricing, stock, and identity fields are never accepted or sent.",
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "string", "description": "Required product UUID."},
            "name": {"type": "string", "description": "Optional product name."}, "subtitle": {"type": "string", "description": "Optional product subtitle."}, "description": {"type": "string", "description": "Optional product description."}, "image": {"type": "string", "description": "Optional product image value."},
            "hscode": {"type": "string", "description": "Optional HS code."}, "coo": {"type": "string", "description": "Optional country-of-origin value."}, "tax_code": {"type": "string", "description": "Optional tax code."}, "classification_code": {"type": "string", "description": "Optional classification code."},
            "weight": {"type": "number", "description": "Optional positive finite weight."}, "weight_unit_code": {"type": "string", "enum": sorted(_PRODUCT_WEIGHT_UNITS), "description": "Optional weight unit."},
            "dimension_length": {"type": "number", "description": "Optional positive finite length."}, "dimension_breadth": {"type": "number", "description": "Optional positive finite breadth."}, "dimension_height": {"type": "number", "description": "Optional positive finite height."}, "measure_unit_code": {"type": "string", "enum": sorted(_PRODUCT_MEASURE_UNITS), "description": "Optional measure unit."},
            "is_fragile": {"type": "boolean", "description": "Optional fragile-product flag."}, "category_id": {"type": "string", "description": "Optional category UUID."},
        },
        "required": ["product_id"],
    },
    destructive=True, open_world=False, load_target=_load_update_product_target, validate=_validate_update_product, build_preview=_preview_update_product, execute=_execute_update_product,
)


_LISTING_UPDATE_FIELDS = (
    "allocation_mode", "allocation", "allocation_fixed", "channel_buffer",
    "min_publish_qty", "max_publish_qty", "zero_quantity_behaviour",
)
_NULLABLE_LISTING_FIELDS = frozenset({"allocation", "min_publish_qty", "max_publish_qty"})
_ALLOCATION_MODES = frozenset({"SHARED", "PERCENT", "FIXED", "UNMANAGED"})
_ZERO_QUANTITY_BEHAVIOURS = frozenset({"PUBLISH_ZERO", "UNPUBLISH"})
_MISSING_LISTING_ENDPOINT = "TODO_CAPTURE_PRODUCT_CHANNEL_LISTING_ENDPOINT"


def _validate_listing_ids(args: dict[str, Any], context: WriteContext) -> None:
    if not context.tenant_id or not str(context.tenant_id).strip():
        raise WriteError("AUTH_REQUIRED", "An authenticated tenant is required.")
    for name in ("product_id", "channel_id"):
        value = args.get(name)
        try:
            UUID(value.strip()) if isinstance(value, str) else (_ for _ in ()).throw(ValueError())
        except ValueError:
            raise WriteError("VALIDATION_ERROR", f"{name} must be a valid UUID.") from None


def _validate_listing_input(args: dict[str, Any], context: WriteContext) -> None:
    _validate_listing_ids(args, context)
    if not any(name in args for name in _LISTING_UPDATE_FIELDS):
        raise WriteError("VALIDATION_ERROR", "At least one listing update field is required.")
    if "external_id" in args and args["external_id"] is not None and not isinstance(args["external_id"], str):
        raise WriteError("VALIDATION_ERROR", "external_id must be a string.")
    for name in _LISTING_UPDATE_FIELDS:
        if name in args and args[name] is None and name not in _NULLABLE_LISTING_FIELDS:
            raise WriteError("VALIDATION_ERROR", f"{name} cannot be null.")
    if "allocation_mode" in args and (not isinstance(args["allocation_mode"], str) or args["allocation_mode"].upper() not in _ALLOCATION_MODES):
        raise WriteError("VALIDATION_ERROR", "allocation_mode is invalid.")
    if "zero_quantity_behaviour" in args and (not isinstance(args["zero_quantity_behaviour"], str) or args["zero_quantity_behaviour"].upper() not in _ZERO_QUANTITY_BEHAVIOURS):
        raise WriteError("VALIDATION_ERROR", "zero_quantity_behaviour is invalid.")
    if "allocation" in args and args["allocation"] is not None and (isinstance(args["allocation"], bool) or not isinstance(args["allocation"], (int, float)) or not math.isfinite(args["allocation"]) or not 0 <= args["allocation"] <= 100):
        raise WriteError("VALIDATION_ERROR", "allocation must be a finite number from 0 to 100 or null.")
    for name in ("allocation_fixed", "channel_buffer"):
        if name in args and (isinstance(args[name], bool) or not isinstance(args[name], (int, float)) or not math.isfinite(args[name]) or args[name] < 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a non-negative finite number.")
    for name in ("min_publish_qty", "max_publish_qty"):
        if name in args and args[name] is not None and (isinstance(args[name], bool) or not isinstance(args[name], int) or args[name] < 0):
            raise WriteError("VALIDATION_ERROR", f"{name} must be a non-negative integer or null.")


async def _verify_listing_target(args: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    _validate_listing_input(args, context)
    api = get_api()
    product_response = await api.async_get(f"/commerce/products/{args['product_id'].strip()}")
    product = product_response.get("data", product_response) if isinstance(product_response, dict) else {}
    if not isinstance(product, dict) or product.get("tenantId") != str(context.tenant_id):
        raise WriteError("NOT_PERMITTED", "The product does not belong to the authenticated tenant or tenant ownership could not be verified.")
    params = {"externalId": args["external_id"]} if args.get("external_id") is not None else None
    try:
        verify_response = await api.async_get(
            f"/commerce/products/{args['product_id'].strip()}/channels/{args['channel_id'].strip()}/settings/verify",
            params=params,
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise WriteError("CAPABILITY_MISSING", "PointNXT product channel-listing read endpoint is not confirmed.", details={"todo": _MISSING_LISTING_ENDPOINT, "capture": ["URL", "HTTP method", "payload field names"]}) from error
        raise
    listing = verify_response.get("data", verify_response) if isinstance(verify_response, dict) else {}
    required = ("id", "tenantId", "productId", "channelId")
    if not isinstance(listing, dict) or any(not listing.get(field) for field in required):
        raise WriteError("NOT_PERMITTED", "The product-channel listing ownership could not be verified.")
    if (
        listing["tenantId"] != str(context.tenant_id)
        or str(listing["productId"]) != args["product_id"].strip()
        or str(listing["channelId"]) != args["channel_id"].strip()
    ):
        raise WriteError("NOT_PERMITTED", "The listing does not belong to the authenticated tenant and requested product/channel.")
    return {"id": str(listing["id"]), "reference": str(listing["id"]), "product_id": args["product_id"].strip(), "channel_id": args["channel_id"].strip()}


def _validate_update_listing(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> None:
    _validate_listing_input(args, context)


def _listing_payload(args: dict[str, Any]) -> dict[str, Any]:
    field_map = {
        "allocation_mode": "allocationMode", "allocation": "allocation", "allocation_fixed": "allocationFixed",
        "channel_buffer": "channelBuffer", "min_publish_qty": "minPublishQty", "max_publish_qty": "maxPublishQty",
        "zero_quantity_behaviour": "zeroQuantityBehaviour",
    }
    return {backend: args[name].upper() if name in ("allocation_mode", "zero_quantity_behaviour") else args[name] for name, backend in field_map.items() if name in args}


def _preview_update_listing(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> dict[str, Any]:
    return {"summary": f"Update channel listing {target['id']}", "proposed_changes": {"product_id": target["product_id"], "channel_id": target["channel_id"], "selected_listing_id": target["id"], **_listing_payload(args)}, "side_effects": ["Allocation and publication settings may affect sellable inventory or channel availability."]}


async def _execute_update_listing(args: dict[str, Any], target: dict[str, Any], context: WriteContext) -> Any:
    return await get_api().async_request("PUT", f"/commerce/products/{target['product_id']}/channels/{target['channel_id']}/settings", json=_listing_payload(args), retry=False)


UPDATE_PRODUCT_CHANNEL_LISTING_SPEC = WriteToolSpec(
    name="update_product_channel_listing",
    description="Preview and, after explicit confirmation, update an existing product channel listing after authenticated tenant and listing verification. Price and MRP are excluded.",
    input_schema={"type": "object", "properties": {
        "product_id": {"type": "string", "description": "Product UUID."}, "channel_id": {"type": "string", "description": "Channel UUID."}, "external_id": {"type": "string", "description": "Optional listing selector."},
        "allocation_mode": {"type": "string", "enum": sorted(_ALLOCATION_MODES), "description": "Allocation mode."}, "allocation": {"type": ["number", "null"], "nullable": True, "description": "Allocation percentage or null to inherit."}, "allocation_fixed": {"type": "number", "description": "Non-negative fixed allocation."}, "channel_buffer": {"type": "number", "description": "Non-negative channel buffer."},
        "min_publish_qty": {"type": ["integer", "null"], "nullable": True, "description": "Minimum publish quantity or null."}, "max_publish_qty": {"type": ["integer", "null"], "nullable": True, "description": "Maximum publish quantity or null."}, "zero_quantity_behaviour": {"type": "string", "enum": sorted(_ZERO_QUANTITY_BEHAVIOURS), "description": "Zero-quantity behavior."},
    }, "required": ["product_id", "channel_id"]},
    destructive=True, open_world=False, load_target=_verify_listing_target, validate=_validate_update_listing, build_preview=_preview_update_listing, execute=_execute_update_listing,
)


async def get_products(
    limit: int = 10,
    page: int = 1,
    sku: str | None = None,
    name: str | None = None,
    status: str | None = None,
    barcode: str | None = None,
    brand_id: str | None = None,
    category_id: str | None = None,
    channel_id: str | None = None,
    vendor_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve products with optional pagination and search filters."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise ValueError("page must be a positive integer")
    if limit > MAX_PRODUCT_PAGE_SIZE:
        raise ValueError(f"limit must be at most {MAX_PRODUCT_PAGE_SIZE}")
    if status is not None and (
        not isinstance(status, str) or status.upper() not in PRODUCT_STATUSES
    ):
        raise ValueError(
            f"Invalid status {status!r}. Valid values: {', '.join(PRODUCT_STATUSES)}"
        )

    params = {
        key: value
        for key, value in {
            "limit": limit,
            "page": page,
            "sku": sku,
            "name": name,
            "status": status,
            "barcode": barcode,
            "brandId": brand_id,
            "categoryId": category_id,
            "channelId": channel_id,
            "vendorId": vendor_id,
            "sellerId": seller_id,
            "warehouseId": warehouse_id,
        }.items()
        if value is not None
    }

    api = get_api()
    try:
        return await api.async_get("/commerce/products", params=params)
    except httpx.HTTPStatusError as error:
        if not sku or error.response.status_code != 400:
            raise
        return await _scan_products_by_sku(api, sku, limit, page)


async def _scan_products_by_sku(
    api: PointNXTAPI, sku: str, limit: int, page: int
) -> dict[str, Any]:
    """Find SKU matches when the backend rejects the unsupported sku filter."""
    matches: list[dict[str, Any]] = []
    scanned_pages = 0
    total_pages: int | None = None
    for scan_page in range(1, PRODUCT_SKU_SCAN_MAX_PAGES + 1):
        response = await api.async_get(
            "/commerce/products",
            params={
                "limit": PRODUCT_SKU_SCAN_PAGE_SIZE,
                "page": scan_page,
            },
        )
        data = response.get("data", {})
        items = data.get("items", []) or []
        matches.extend(
            item for item in items
            if isinstance(item, dict)
            and str(item.get("sku", "")).strip().casefold() == sku.strip().casefold()
        )
        scanned_pages = scan_page
        metadata = data.get("metadata", {}) or {}
        total_items = metadata.get("totalItems", data.get("totalItems"))
        if isinstance(total_items, int) and total_items >= 0:
            total_pages = max(
                1, (total_items + PRODUCT_SKU_SCAN_PAGE_SIZE - 1)
                // PRODUCT_SKU_SCAN_PAGE_SIZE
            )
        if not items or (total_pages is not None and scan_page >= total_pages):
            break

    start = (page - 1) * limit
    end = start + limit
    partial = scanned_pages >= PRODUCT_SKU_SCAN_MAX_PAGES and (
        total_pages is None or scanned_pages < total_pages
    )
    metadata = {
        "page": page,
        "limit": limit,
        "totalItems": len(matches),
        "total_is_partial": partial,
    }
    return {"data": {"items": matches[start:end], "metadata": metadata}}


async def get_product_summary() -> dict[str, Any]:
    """Retrieve product and inventory summary metrics from the backend."""
    response = await get_api().async_get("/commerce/products/summary")
    return {**response, "count_basis": PRODUCT_SUMMARY_COUNT_BASIS}


async def get_product_by_id(product_id: str) -> dict[str, Any]:
    """Retrieve complete details for one product by ID."""

    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")

    return await get_api().async_get(f"/commerce/products/{product_id}")


def _name(value: Any, *fallbacks: Any) -> str | None:
    for candidate in (value, *fallbacks):
        if isinstance(candidate, dict):
            candidate = candidate.get("name") or candidate.get("label")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _stock_rows(product: dict[str, Any]) -> list[dict[str, Any]]:
    raw = product.get("stocks") if "stocks" in product else product.get("inventory")
    if isinstance(raw, dict):
        nested = raw.get("warehouses") or raw.get("items") or raw.get("stocks")
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
        if raw.get("tracked") is False:
            return []
        return [raw]
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def _explicit_inventory_tracking(product: dict[str, Any]) -> bool | None:
    inventory = product.get("inventory")
    if isinstance(inventory, dict) and isinstance(inventory.get("tracked"), bool):
        return inventory["tracked"]
    for key in (
        "inventoryTracked",
        "trackInventory",
        "inventoryManaged",
        "isInventoryManaged",
        "manageInventory",
    ):
        if isinstance(product.get(key), bool):
            return product[key]
    return None


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return 0
        return int(parsed) if parsed.is_integer() else parsed
    return 0


def _compact_inventory(product: dict[str, Any]) -> dict[str, Any] | None:
    rows = _stock_rows(product)
    tracking = _explicit_inventory_tracking(product)
    if tracking is False:
        return {"tracked": False}
    if not rows:
        return None

    warehouses: dict[str, dict[str, Any]] = {}
    on_hand = available = reserved = 0
    for row in rows:
        warehouse = row.get("warehouse")
        code = (
            row.get("warehouseCode")
            or row.get("warehouse_code")
            or (warehouse.get("code") if isinstance(warehouse, dict) else None)
            or row.get("warehouseId")
            or "unknown"
        )
        row_on_hand = _number(row.get("quantityOnHand", row.get("onHand", 0)))
        row_available = _number(row.get("quantityAvailable", row.get("available", 0)))
        row_reserved = _number(row.get("quantityReserved", row.get("reserved", 0)))
        on_hand += row_on_hand
        available += row_available
        reserved += row_reserved
        current = warehouses.setdefault(
            code, {"warehouse_code": code, "on_hand": 0, "available": 0, "reserved": 0}
        )
        current["on_hand"] += row_on_hand
        current["available"] += row_available
        current["reserved"] += row_reserved
    return {
        "tracked": True,
        "on_hand": on_hand,
        "available": available,
        "reserved": reserved,
        "by_warehouse": list(warehouses.values()),
    }


def _compact_product(product: dict[str, Any]) -> dict[str, Any]:
    inventory = _compact_inventory(product)
    compact = {
        "id": product.get("id"),
        "sku": product.get("sku"),
        "name": product.get("name"),
        "status": product.get("status"),
        "barcode": product.get("barcode"),
        "brand": _name(product.get("brand"), product.get("brandName")),
        "category": _name(product.get("category"), product.get("categoryName")),
        "vendor": _name(product.get("vendor"), product.get("vendorName")),
        "inventory": inventory,
    }
    if inventory is None:
        compact["inventory_note"] = (
            "Inventory data was not included in this backend response; tracking status is unknown."
        )
    return compact


async def _enrich_inventory(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill missing list inventory from the existing product-detail endpoint."""
    candidates = [
        product
        for product in products
        if product.get("id")
        and _compact_inventory(product) is None
        and _explicit_inventory_tracking(product) is not False
    ]
    if not candidates:
        return products

    async def fetch(product: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await get_product_by_id(str(product["id"]))
            detail = response.get("data", response)
            return {**product, **detail} if isinstance(detail, dict) else product
        except Exception:
            logger.warning("Product inventory enrichment failed", exc_info=True)
            return product

    enriched_candidates = await asyncio.gather(
        *(fetch(product) for product in candidates)
    )
    by_id = {product.get("id"): product for product in enriched_candidates}
    return [by_id.get(product.get("id"), product) for product in products]


async def search_products(
    limit: int = 20,
    page: int = 1,
    name: str | None = None,
    sku: str | None = None,
    product_id: str | None = None,
    status: str | None = None,
    barcode: str | None = None,
    brand_id: str | None = None,
    category_id: str | None = None,
    channel_id: str | None = None,
    vendor_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """Search products through the existing catalog endpoint."""
    if status is not None and (
        not isinstance(status, str) or status.upper() not in PRODUCT_STATUSES
    ):
        raise ValueError(
            f"Invalid status {status!r}. Valid values: {', '.join(PRODUCT_STATUSES)}"
        )
    if product_id:
        response = await get_product_by_id(product_id)
        product = response.get("data", response)
        return {"found": bool(product), "products": [_compact_product(product)] if product else []}
    response = await get_products(
        limit=limit, page=page, sku=sku, name=name, status=status, barcode=barcode,
        brand_id=brand_id, category_id=category_id, channel_id=channel_id,
        vendor_id=vendor_id, seller_id=seller_id, warehouse_id=warehouse_id,
    )
    data = response.get("data", {})
    items = data.get("items", []) or []
    items = await _enrich_inventory(items)
    return {
        "found": bool(items),
        "products": [_compact_product(product) for product in items],
        "pagination": data.get("metadata", {}),
        "count_basis": PRODUCT_LIST_COUNT_BASIS,
        "message": None if items else "No matching products found.",
    }


async def get_product(product_id: str) -> dict[str, Any]:
    """Return compact details for one product."""
    response = await get_product_by_id(product_id)
    product = response.get("data", response)
    return {"found": bool(product), "product": _compact_product(product) if product else None}


async def search_inventory(
    limit: int = 20,
    page: int = 1,
    sku: str | None = None,
    name: str | None = None,
    product_id: str | None = None,
    status: str | None = None,
    warehouse_id: str | None = None,
) -> dict[str, Any]:
    """Query inventory fields returned by the existing product services.

    No stock values are calculated here; availability remains backend-owned.
    """
    result = await search_products(
        limit=limit, page=page, sku=sku, name=name, product_id=product_id,
        status=status, warehouse_id=warehouse_id,
    )
    inventory = []
    for product in result.get("products", []):
        value = product.get("inventory")
        inventory.append({
            "product_id": product.get("id"),
            "sku": product.get("sku"),
            "name": product.get("name"),
            "status": product.get("status"),
            "inventory": value,
            "inventory_available": value is not None,
        })
    return {
        "found": bool(inventory),
        "inventory": inventory,
        "pagination": result.get("pagination", {}),
        "message": None if inventory else "No matching inventory records found.",
    }


async def get_inventory_summary() -> dict[str, Any]:
    """Return the backend-owned product/inventory summary without recalculation."""
    response = await get_product_summary()
    data = response.get("data", response)
    return {
        "found": bool(data),
        "summary": data if data else None,
        "message": None if data else "Inventory summary is unavailable.",
    }
