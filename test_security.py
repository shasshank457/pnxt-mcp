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


def assert_tool_annotations() -> None:
    from server import mcp

    names = set(mcp._tool_manager._tools)
    local_read_only = {
        "current_user",
        "auth_status",
        "check_authentication",
        "check_configuration",
        "get_mcp_metrics",
    }
    assert "authenticate_with_pointnxt" not in names
    assert "login_with_credentials" not in names
    expected_read_only = {
        name: (True, False, name not in local_read_only)
        for name in names
        if name != "logout_pointnxt"
    }
    for name, (read_only, destructive, open_world) in expected_read_only.items():
        annotations = mcp._tool_manager._tools[name].annotations
        assert annotations is not None, name
        assert annotations.read_only_hint is read_only, name
        assert annotations.destructive_hint is destructive, name
        assert annotations.open_world_hint is open_world, name

    for name in ("logout_pointnxt",):
        annotations = mcp._tool_manager._tools[name].annotations
        assert annotations is not None, name
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is False, name
        assert annotations.open_world_hint is True, name


def test_tool_annotations() -> None:
    assert_tool_annotations()


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

    assert_tool_annotations()
    print("Security regression tests passed")


if __name__ == "__main__":
    asyncio.run(main())
