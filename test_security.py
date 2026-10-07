"""Security regression tests for request isolation and safe diagnostics."""

import asyncio
import io
import logging
from unittest.mock import AsyncMock, patch

from services.auth_session import current_user, set_session
from services.oauth_client_metadata import ClientMetadataError, _public_addresses
from services.pointnxt_api import _safe_params, _structured_log
from services.request_context import reset_request_key, set_request_key
from services.session_store import clear_all_for_tests
from tools.business import get_business_summary


async def main() -> None:
    clear_all_for_tests()
    first = set_request_key("client-a")
    set_session({"data": {"accessToken": "token-a", "tenantId": "tenant-a"}})
    assert current_user()["tenant_id"] == "tenant-a"
    reset_request_key(first)
    second = set_request_key("client-b")
    assert current_user()["authenticated"] is False
    set_session({"data": {"accessToken": "token-b", "tenantId": "tenant-b"}})
    assert current_user()["tenant_id"] == "tenant-b"
    reset_request_key(second)
    clear_all_for_tests()

    safe = _safe_params({"email": "alice@example.com", "phone": "+15551234567", "search": "alice@example.com"})
    assert "alice@example.com" not in str(safe)
    assert "+15551234567" not in str(safe)
    assert safe["email"].startswith("sha256:")
    assert "alice@example.com" not in str(safe)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("services.pointnxt_api")
    logger.addHandler(handler)
    try:
        _structured_log(logging.INFO, "test", params=safe)
        handler.flush()
        assert "alice@example.com" not in stream.getvalue()
    finally:
        logger.removeHandler(handler)

    with patch("tools.business.get_orders", new=AsyncMock(side_effect=RuntimeError("orders down"))), \
         patch("tools.business.get_order_stats", new=AsyncMock(return_value={"data": {"ok": True}})), \
         patch("tools.business.get_product_summary", new=AsyncMock(return_value={"data": {"ok": True}})):
        summary = await get_business_summary("2026-09-30")
        assert summary["orders"]["count"] == 0
        assert summary["unavailable"][0]["section"] == "orders"
        assert summary["unavailable"][0]["error_code"] == "section_failed"
        assert "orders down" not in str(summary)

    # Restricted metadata destinations are rejected before any HTTP request.
    for address in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.1.1", "::1", "ff02::1"):
        with patch("services.oauth_client_metadata.socket.getaddrinfo", return_value=[(None, None, None, None, (address, 443))]):
            try:
                _public_addresses("metadata.example")
            except ClientMetadataError:
                pass
            else:
                raise AssertionError(f"restricted metadata address accepted: {address}")

    from server import mcp
    names = set(mcp._tool_manager._tools)
    for name in ("get_orders", "find_customer", "search_products", "search_returns", "get_business_summary"):
        assert name in names
    print("Security regression tests passed")


if __name__ == "__main__":
    asyncio.run(main())
