from typing import Any

from services.pointnxt_api import PointNXTAPI


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
