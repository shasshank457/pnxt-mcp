from typing import Any

from services.pointnxt_api import PointNXTAPI

MAX_PRODUCT_PAGE_SIZE = 100


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

    return await get_api().async_get("/commerce/products/summary")


async def get_product_by_id(product_id: str) -> dict[str, Any]:
    """Retrieve complete details for one product by ID."""

    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")

    return await get_api().async_get(f"/commerce/products/{product_id}")


def _compact_product(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": product.get("id"),
        "sku": product.get("sku"),
        "name": product.get("name"),
        "status": product.get("status"),
        "barcode": product.get("barcode"),
        "brand": product.get("brand"),
        "category": product.get("category"),
        "vendor": product.get("vendor"),
        "inventory": product.get("inventory", product.get("stocks")),
    }


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
    return {
        "found": bool(items),
        "products": [_compact_product(product) for product in items],
        "pagination": data.get("metadata", {}),
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
