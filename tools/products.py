import asyncio
import logging
from typing import Any

from services.pointnxt_api import PointNXTAPI
from tools.status_options import PRODUCT_STATUSES

MAX_PRODUCT_PAGE_SIZE = 100
PRODUCT_LIST_COUNT_BASIS = "Paginated products returned by /commerce/products."
PRODUCT_SUMMARY_COUNT_BASIS = (
    "Backend aggregate product summary; its scope may differ from product-list pages."
)
logger = logging.getLogger(__name__)


def get_api() -> PointNXTAPI:
    return PointNXTAPI()


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

    return await get_api().async_get("/commerce/products", params=params)


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
