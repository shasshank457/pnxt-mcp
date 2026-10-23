from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tools import products


@pytest.mark.asyncio
async def test_sku_search_falls_back_when_backend_rejects_sku_filter(monkeypatch):
    api = MagicMock()

    async def async_get(endpoint, params=None):
        if params.get("sku"):
            request = httpx.Request("GET", "https://api.example/commerce/products")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("bad sku filter", request=request, response=response)
        return {
            "data": {
                "items": [
                    {"id": "other", "sku": "OTHER-01"},
                    {"id": "target", "sku": "Port-Cal-001"},
                ],
                "metadata": {"totalItems": 2},
            }
        }

    api.async_get = AsyncMock(side_effect=async_get)
    monkeypatch.setattr(products, "get_api", lambda: api)

    result = await products.get_products(sku="port-cal-001", limit=10)

    assert result["data"]["items"] == [{"id": "target", "sku": "Port-Cal-001"}]
    assert result["data"]["metadata"]["totalItems"] == 1
    assert api.async_get.await_count == 2
