"""Baseline regression tests for the MCP audit findings.

The checked-out project is a Python API-backed MCP client, not the
TypeScript/Prisma server described by the original audit. These tests use
API-shaped fixture records and intentionally assert the audit's desired
behavior without changing production handlers.
"""

import json
import os
import re

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from test.fixtures.mcp_audit_seed import AUDIT_SEED, response_for
from test.mcp_audit_helper import call_tool


@pytest.fixture
def audit_backend(monkeypatch):
    import server
    from services.pointnxt_api import PointNXTAPI

    requests = []

    async def async_get(_self, endpoint, params=None):
        requests.append((endpoint, dict(params or {})))
        return response_for(endpoint, params)

    def get(_self, endpoint, params=None):
        requests.append((endpoint, dict(params or {})))
        return response_for(endpoint, params)

    monkeypatch.setattr(PointNXTAPI, "async_get", async_get)
    monkeypatch.setattr(PointNXTAPI, "get", get)
    monkeypatch.setattr(
        server,
        "run_backend_health_check",
        lambda: {"status": "healthy", "reachable": True, "error": None},
    )
    monkeypatch.setattr(
        server,
        "run_authentication_check",
        lambda: {"status": "unhealthy", "authenticated": False},
    )
    monkeypatch.setattr(
        server,
        "run_configuration_check",
        lambda: {"configured": True, "missing": []},
    )
    return {"seed": AUDIT_SEED, "requests": requests}


def _tool_names():
    from server import mcp

    return {tool.name for tool in mcp._tool_manager.list_tools()}


_FORBIDDEN_KEYS = {
    "config",
    "raw",
    "customer_ip_address",
    "payment_url",
    "order_key",
    "cart_hash",
    "address_1",
    "address_2",
}
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+\d{10,15}|\d{10})(?!\d)")


def _contract_violations(tool_name, response, args):
    encoded = json.dumps(response, default=str)
    violations = []

    def walk(value, path="$"):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold()
                if normalized in _FORBIDDEN_KEYS:
                    violations.append(f"{tool_name}: forbidden response key at {path}.{key}")
                if normalized == "channelcustomerid" and "@" in str(child):
                    violations.append(f"{tool_name}: unmasked channel customer value at {path}.{key}")
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(response)
    for token in (
        "prisma",
        "invalid input syntax",
        "tenantId",
        "customer_ip_address",
        "payment_url",
        "order_key",
        "cart_hash",
    ):
        if token.casefold() in encoded.casefold():
            violations.append(f"{tool_name}: response contains {token!r}")
    if _EMAIL_PATTERN.search(encoded):
        violations.append(f"{tool_name}: response contains an unmasked email")
    if _PHONE_PATTERN.search(encoded):
        violations.append(f"{tool_name}: response contains an unmasked phone")
    if "limit" in args and args["limit"] <= 5 and len(encoded.encode("utf-8")) >= 8 * 1024:
        violations.append(f"{tool_name}: limit<=5 response is at least 8 KB")
    return violations


def _tool_error_contract_violations(tool_name, error):
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    if (
        not isinstance(code, (str, int))
        or not isinstance(message, str)
        or not message.strip()
        or re.fullmatch(r"[a-z]+(?:\.[a-zA-Z]+)+", message.strip())
    ):
        return [f"{tool_name}: error is not a structured {{code, message}}"]
    return []


@pytest.mark.asyncio
async def test_registered_tool_contracts(audit_backend):
    """Run all exposed handlers and check response safety and bounded size."""
    from server import mcp

    args_by_name = {
        "get_order_by_id": {"order_id": "order-woo"},
        "get_order": {"order_reference": "AUDIT-100"},
        "get_product_by_id": {"product_id": "product-audit"},
        "get_product": {"product_id": "product-audit"},
        "get_order_shipments": {"order_reference": "AUDIT-100"},
        "get_order_returns": {"order_reference": "AUDIT-100"},
        "find_customer": {"name": "Audit Customer"},
        "get_customer_context": {"customer_id": "customer-audit"},
    }
    violations = []
    for tool in mcp._tool_manager.list_tools():
        args = dict(args_by_name.get(tool.name, {}))
        if "limit" in tool.parameters.get("properties", {}):
            args["limit"] = 5
        try:
            response = await call_tool(tool.name, args)
        except ToolError as error:
            violations.extend(_tool_error_contract_violations(tool.name, error))
            continue
        violations.extend(_contract_violations(tool.name, response, args))
    assert not violations, "\n".join(violations)


@pytest.mark.asyncio
async def test_tool_errors_use_structured_human_readable_shape(audit_backend):
    with pytest.raises(ToolError) as raised:
        await call_tool("get_orders", {"order_status": "NOT_A_STATUS"})
    error = raised.value
    assert isinstance(getattr(error, "code", None), str)
    assert isinstance(getattr(error, "message", None), str)
    assert error.message.strip()
    assert not error.message.startswith("order.error.")


class TestBug01OrderPII:
    @pytest.mark.asyncio
    async def test_get_order_omits_raw_woocommerce_pii(self, audit_backend):
        response = await call_tool("get_order", {"order_reference": "AUDIT-100"})
        violations = _contract_violations("get_order", response, {})
        assert not violations, "\n".join(violations)


class TestBug02DetailSanitization:
    @pytest.mark.asyncio
    async def test_detail_tools_do_not_return_raw_rows(self, audit_backend):
        order = await call_tool("get_order_by_id", {"order_id": "order-woo"})
        product = await call_tool("get_product_by_id", {"product_id": "product-audit"})
        for response in (order, product):
            encoded = json.dumps(response)
            assert "tenantId" not in encoded
            assert "config" not in encoded
            assert "<p>" not in encoded


class TestBug03CustomerAggregates:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_top_customers")

    @pytest.mark.asyncio
    async def test_customer_aggregate_tool_is_registered(self, audit_backend):
        assert "get_top_customers" in _tool_names()


class TestBug04InventoryCostFallback:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_inventory_value")

    @pytest.mark.asyncio
    async def test_inventory_value_tool_is_registered(self, audit_backend):
        assert "get_inventory_value" in _tool_names()


class TestBug05WooCommerceWeightImport:
    pytestmark = pytest.mark.skip(reason="tool not in this server: import_channel_product")

    def test_channel_import_tool_is_registered(self):
        assert "import_channel_product" in _tool_names()


class TestBug06ChannelSuspensionState:
    pytestmark = pytest.mark.skip(reason="tool not in this server: list_channels")

    @pytest.mark.asyncio
    async def test_channel_listing_tool_is_registered(self, audit_backend):
        assert "list_channels" in _tool_names()


class TestBug07OrderHistoryAndShipments:
    @pytest.mark.asyncio
    async def test_fulfilled_order_has_lifecycle_timestamps_and_shipment(self, audit_backend):
        response = await call_tool("get_order", {"order_reference": "AUDIT-100"})
        order = response["order"]
        assert order.get("confirmed_at")
        assert order.get("fulfilled_at")
        assert order["shipments"]


class TestBug08ChannelSyncAttribution:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_channel_sync_health")

    @pytest.mark.asyncio
    async def test_channel_sync_health_is_registered(self, audit_backend):
        assert "get_channel_sync_health" in _tool_names()


class TestBug09TopProductsUnmappedLines:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_top_products")

    @pytest.mark.asyncio
    async def test_top_products_tool_is_registered(self, audit_backend):
        assert "get_top_products" in _tool_names()


class TestBug10ReversedDateRange:
    @pytest.mark.asyncio
    async def test_reversed_date_range_is_rejected(self, audit_backend):
        with pytest.raises(ToolError, match="start_date|date range"):
            await call_tool(
                "get_orders",
                {"start_date": "2026-10-12", "end_date": "2026-10-10"},
            )
        assert audit_backend["requests"] == []


class TestBug11ReadableErrors:
    @pytest.mark.asyncio
    async def test_validation_errors_are_not_i18n_keys(self, audit_backend):
        with pytest.raises(ToolError) as raised:
            await call_tool("get_orders", {"order_status": "NOT_A_STATUS"})
        assert "order.error." not in str(raised.value)


class TestBug12DailyTrend:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_daily_order_trend")

    @pytest.mark.asyncio
    async def test_daily_trend_tool_is_registered(self, audit_backend):
        assert "get_daily_order_trend" in _tool_names()


class TestBug13SlaAgeFilter:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_sla_breaches")

    @pytest.mark.asyncio
    async def test_sla_breach_tool_is_registered(self, audit_backend):
        assert "get_sla_breaches" in _tool_names()


class TestBug14WarehouseAndShipmentErrors:
    @pytest.mark.asyncio
    @pytest.mark.skip(reason="tool not in this server: get_warehouse")
    async def test_warehouse_reference_resolution(self, audit_backend):
        await call_tool("get_warehouse", {"warehouse_code": "DDH"})

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="tool not in this server: track_shipment")
    async def test_shipment_reference_resolution(self, audit_backend):
        await call_tool("track_shipment", {"orderId": "1000002101"})


class TestBug15TenantTimezone:
    @pytest.mark.asyncio
    async def test_date_filters_use_tenant_local_day_boundaries(self, audit_backend):
        await call_tool(
            "get_orders",
            {"start_date": "2026-10-11", "end_date": "2026-10-11"},
        )
        endpoint, params = audit_backend["requests"][-1]
        assert endpoint == "/commerce/orders"
        assert params["dateFrom"] == "2026-10-10T18:30:00.000Z"
        assert params["dateTo"] == "2026-10-11T18:29:59.999Z"


class TestBug16WalletLedger:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_wallet")

    @pytest.mark.asyncio
    async def test_wallet_ledger_tool_is_registered(self, audit_backend):
        assert "get_wallet" in _tool_names()


class TestBug17InvoiceTotals:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_invoice")

    @pytest.mark.asyncio
    async def test_invoice_tool_is_registered(self, audit_backend):
        assert "get_invoice" in _tool_names()


class TestBug18StockMovementLedger:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_stock_movements")

    @pytest.mark.asyncio
    async def test_stock_movement_tool_is_registered(self, audit_backend):
        assert "get_stock_movements" in _tool_names()


class TestBug19ListingIssues:
    pytestmark = pytest.mark.skip(reason="tool not in this server: get_listing_issues")

    @pytest.mark.asyncio
    async def test_listing_issues_tool_is_registered(self, audit_backend):
        assert "get_listing_issues" in _tool_names()


class TestBug20CourierPerformance:
    @pytest.mark.asyncio
    @pytest.mark.skip(reason="tool not in this server: get_courier_performance")
    async def test_courier_performance_unassigned_courier(self, audit_backend):
        await call_tool("get_courier_performance")

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="tool not in this server: get_rto_summary")
    async def test_rto_summary_unassigned_courier(self, audit_backend):
        await call_tool("get_rto_summary")


class TestBug21LimitValidation:
    @pytest.mark.asyncio
    async def test_huge_limit_is_rejected(self, audit_backend):
        with pytest.raises(ToolError, match="limit must be at most"):
            await call_tool("get_orders", {"limit": 1000000})
        assert audit_backend["requests"] == []


class TestBug22MoneyRounding:
    @pytest.mark.asyncio
    async def test_summary_money_is_rounded(self, audit_backend):
        response = await call_tool("get_orders_summary", {"limit": 5})
        assert response["total_order_value"] == round(response["total_order_value"], 2)
        assert response["average_order_value"] == round(
            response["average_order_value"], 2
        )


class TestBug23EmptyResponseContext:
    @pytest.mark.asyncio
    async def test_empty_search_response_has_context(self, audit_backend, monkeypatch):
        from services.pointnxt_api import PointNXTAPI

        async def empty_orders(_self, endpoint, params=None):
            audit_backend["requests"].append((endpoint, dict(params or {})))
            return {
                "data": {
                    "items": [],
                    "totalItems": 0,
                    "metadata": {"page": 1, "limit": 20, "totalItems": 0},
                }
            }

        monkeypatch.setattr(PointNXTAPI, "async_get", empty_orders)
        response = await call_tool("search_orders", {"order_reference": "MISSING"})
        assert isinstance(response, dict)
        assert response.get("found") is False
        assert response.get("orders") == []
        assert response.get("message") or response.get("note")


class TestBug24TestOrderAnalytics:
    @pytest.mark.asyncio
    async def test_test_order_fixture_is_excluded_from_analytics(self, audit_backend):
        assert any(order.get("isTest") for order in AUDIT_SEED["orders"])
        response = await call_tool("get_orders_summary", {"limit": 5})
        assert response["total_orders"] == sum(
            not order.get("isTest", False) for order in AUDIT_SEED["orders"]
        )


_LIVE_REQUIRED_TOOLS = (
    "get_order",
    "get_warehouse",
    "track_shipment",
    "get_inventory_value",
)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_mcp_smoke_contract(monkeypatch):
    """Opt-in smoke test against the configured real PointNXT API."""
    api_url = os.getenv("POINTNXT_API_URL")
    token = os.getenv("POINTNXT_TOKEN")
    if not api_url or not token:
        pytest.skip("set POINTNXT_API_URL and POINTNXT_TOKEN to enable live smoke")

    missing_tools = set(_LIVE_REQUIRED_TOOLS) - _tool_names()
    if missing_tools:
        pytest.skip(
            "tool not in this server: " + ", ".join(sorted(missing_tools))
        )

    from services import pointnxt_api
    from services.request_context import reset_request_key, set_request_key

    monkeypatch.setattr(pointnxt_api, "POINTNXT_BASE_URL", api_url)
    monkeypatch.setattr(pointnxt_api, "POINTNXT_ACCESS_TOKEN", token)
    monkeypatch.setattr(pointnxt_api, "DEV_MODE", True)
    tenant_id = os.getenv("POINTNXT_TENANT_ID")
    if tenant_id:
        monkeypatch.setattr(pointnxt_api, "POINTNXT_TENANT_ID", tenant_id)
    request_token = set_request_key("stdio")
    args = {
        "get_order": {"order_reference": "1000002101", "orderId": "1000002101"},
        "get_warehouse": {"warehouse_code": "DDH", "code": "DDH"},
        "track_shipment": {"orderId": "1000002101"},
        "get_inventory_value": {},
    }
    try:
        for name in _LIVE_REQUIRED_TOOLS:
            from server import mcp

            registered_tool = mcp._tool_manager._tools[name]
            accepted = registered_tool.parameters.get("properties", {})
            call_args = {
                key: value
                for key, value in args[name].items()
                if key in accepted
            }
            for key, schema in accepted.items():
                if key not in call_args and "default" not in schema:
                    if name == "get_warehouse":
                        call_args[key] = "DDH"
                    elif name in {"get_order", "track_shipment"}:
                        call_args[key] = "1000002101"
            try:
                response = await call_tool(name, call_args)
            except ToolError as error:
                violations = _tool_error_contract_violations(name, error)
                assert not violations, "\n".join(violations)
                continue
            violations = _contract_violations(name, response, call_args)
            assert not violations, "\n".join(violations)
    finally:
        reset_request_key(request_token)
