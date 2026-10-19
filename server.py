import inspect
import logging
from functools import wraps
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from config import (
    MCP_FORWARDED_ALLOW_IPS,
    validate_mcp_oauth_configuration,
    validate_write_configuration,
)
from prompts import register_prompts
from services import auth_bridge, session_store
from services.mcp_oauth import (
    authorize as oauth_authorize,
)
from services.mcp_oauth import (
    callback as oauth_callback,
)
from services.mcp_oauth import consent as oauth_consent
from services.mcp_oauth import consent_decision as oauth_consent_decision
from services.mcp_oauth import (
    metadata_authorization_server,
    metadata_protected_resource,
)
from services.mcp_oauth import (
    revoke as oauth_revoke,
)
from services.mcp_oauth import (
    token as oauth_token,
)
from services.mcp_protocol_gate import MCPProtocolGate
from services.mcp_session_middleware import MCPSessionMiddleware
from services.metrics import get_metrics
from services.response_safety import sanitize_public_response
from services.tool_errors import StructuredToolError
from services.write_tools import register_enabled_write_tools
from tools.auth import auth_callback, auth_check
from tools.auth import current_user as get_current_user
from tools.business import (
    get_attention_queue as fetch_attention_queue,
)
from tools.business import (
    get_business_summary as fetch_business_summary,
)
from tools.customers import (
    find_customers,
)
from tools.customers import (
    get_customer_context as fetch_customer_context,
)
from tools.orders import (
    get_order as fetch_order,
)
from tools.orders import (
    get_order_by_id as fetch_order_by_id,
)
from tools.orders import (
    get_order_stats as fetch_order_stats,
)
from tools.orders import (
    get_orders as fetch_orders,
)
from tools.orders import (
    get_orders_summary as fetch_orders_summary,
)
from tools.orders import (
    get_status_transitions as fetch_status_transitions,
)
from tools.orders import (
    search_orders as find_orders,
)
from tools.products import (
    get_inventory_summary as fetch_inventory_summary,
)
from tools.products import (
    get_product as fetch_product,
)
from tools.products import (
    get_product_by_id as fetch_product_by_id,
)
from tools.products import (
    get_product_summary as fetch_product_summary,
)
from tools.products import (
    get_products as fetch_products,
)
from tools.products import (
    search_inventory as find_inventory,
)
from tools.products import (
    search_products as find_products,
)
from tools.returns import (
    get_order_returns as fetch_order_returns,
)
from tools.returns import (
    search_returns as find_returns,
)
from tools.shipments import (
    get_fulfillment_queue as fetch_fulfillment_queue,
)
from tools.shipments import (
    get_order_shipments as fetch_order_shipments,
)
from tools.shipments import (
    search_shipments as find_shipments,
)
from tools.status_options import (
    ORDER_STATUSES,
    PRODUCT_STATUSES,
    RETURN_STATUSES,
    SHIP_STATUSES,
)
from tools.system import (
    check_authentication as run_authentication_check,
)
from tools.system import (
    check_backend_health as run_backend_health_check,
)
from tools.system import (
    check_configuration as run_configuration_check,
)

# Tool annotations are explicit because OpenAI's public MCP review requires all
# three behavior hints on every exposed tool.
READ_ONLY_EXTERNAL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, openWorldHint=False
)
READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, openWorldHint=False
)

# Pydantic uses these annotations to describe every exposed argument in the MCP
# JSON schema.  The values remain strings at runtime so existing tool behavior is
# unchanged, while status fields advertise the same options used by validation.
Limit = Annotated[int, Field(description="Number of records to return; 1-100.")]
Page = Annotated[int, Field(description="1-based page number.")]
Text = Annotated[str | None, Field(description="Optional text filter; matching behavior depends on the tool.")]
Identifier = Annotated[str | None, Field(description="Optional PointNXT identifier used to narrow the search.")]
Date = Annotated[str | None, Field(description="Optional calendar date in YYYY-MM-DD format, interpreted in the selected timezone.")]
Timezone = Annotated[str | None, Field(description="Optional IANA timezone name; defaults to Asia/Kolkata.")]
RequiredId = Annotated[str, Field(description="PointNXT record identifier.")]
Reference = Annotated[str, Field(description="Order number or channel order number; leading # is accepted.")]
Email = Annotated[str | None, Field(description="Optional customer email; trim and lowercase matching is applied.")]
Phone = Annotated[str | None, Field(description="Optional customer phone; non-digits are ignored and the last 10 digits are matched.")]
Name = Annotated[str | None, Field(description="Optional customer or product name search; matching follows the backend search behavior.")]
OrderStatus = Annotated[
    str | None,
    Field(description="PointNXT order status.", json_schema_extra={"enum": list(ORDER_STATUSES)}),
]
ReturnStatus = Annotated[
    str | None,
    Field(description="Return-related order status.", json_schema_extra={"enum": list(RETURN_STATUSES)}),
]
ProductStatus = Annotated[
    str | None,
    Field(description="Product lifecycle status.", json_schema_extra={"enum": list(PRODUCT_STATUSES)}),
]
ShipStatus = Annotated[
    str | None,
    Field(description="Stored shipment status.", json_schema_extra={"enum": list(SHIP_STATUSES)}),
]
Flag = Annotated[bool, Field(description="Whether to include the broader result set.")]

# Global MCP server object
mcp = MCPServer("PointNXT MCP", middleware=[MCPSessionMiddleware()])
logger = logging.getLogger(__name__)
register_prompts(mcp)


@mcp.custom_route("/auth/callback", methods=["GET"])
async def pointnxt_auth_callback(request):
    if session_store.has_oauth_transaction(request.query_params.get("state", "")):
        return await oauth_callback(request)
    if request.query_params.get("bridge") == "1":
        return await auth_bridge.callback(request)
    return await auth_callback(request)


@mcp.custom_route("/auth/login", methods=["GET", "POST"])
async def pointnxt_auth_bridge(request):
    if request.method == "GET":
        return await auth_bridge.login_page(request)
    return await auth_bridge.login_submit(request)


@mcp.custom_route("/auth/check", methods=["GET"])
async def pointnxt_auth_check(request):
    return await auth_check(request)


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request):
    return await metadata_protected_resource(request)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(request):
    return await metadata_authorization_server(request)


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize_route(request):
    return await oauth_authorize(request)


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback_route(request):
    return await oauth_callback(request)


@mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
async def oauth_consent_route(request):
    if request.method == "GET":
        return await oauth_consent(request)
    return await oauth_consent_decision(request)


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token_route(request):
    return await oauth_token(request)


@mcp.custom_route("/oauth/revoke", methods=["POST"])
async def oauth_revoke_route(request):
    return await oauth_revoke(request)


@mcp.tool(annotations=READ_ONLY_LOCAL)
def current_user():
    """Return the currently authenticated PointNXT user without exposing tokens. Use this for identity context and use check_authentication for a lightweight validity check. The response is read-only."""
    return get_current_user()


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def auth_status():
    """Return lightweight authentication status without exposing tokens. Use this before protected business tools when authentication may be missing. It does not initiate login or change the session."""
    user = get_current_user()
    return {
        "authenticated": user.get("authenticated", False),
        "user": user.get("user_id"),
        "email": user.get("email"),
        "tenant": user.get("tenant_id"),
        "expires_in_seconds": user.get("expires_in_seconds"),
    }


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_orders(
    limit: Limit = 10,
    page: Page = 1,
    order_status: OrderStatus = None,
    start_date: Date = None,
    end_date: Date = None,
    payment_method: Text = None,
    channel_id: Identifier = None,
    seller_id: Identifier = None,
    warehouse_id: Identifier = None,
    customer_id: Identifier = None,
    order_no: Text = None,
    customer_email: Email = None,
    customer_phone: Phone = None,
    channel_order_id: Identifier = None,
    timezone: Timezone = None,
):
    """Retrieve read-only PointNXT orders with pagination and optional filters.
    Use this for broad order lists; use search_orders for customer-oriented searches and get_order for one compact order. Dates are YYYY-MM-DD values interpreted in the selected IANA timezone (default Asia/Kolkata), status must be a listed enum, and limit is 1-100."""
    response = await fetch_orders(
        limit=limit,
        page=page,
        order_status=order_status,
        start_date=start_date,
        end_date=end_date,
        payment_method=payment_method,
        channel_id=channel_id,
        seller_id=seller_id,
        warehouse_id=warehouse_id,
        customer_id=customer_id,
        order_no=order_no,
        customer_email=customer_email,
        customer_phone=customer_phone,
        channel_order_id=channel_order_id,
        timezone=timezone,
    )
    return sanitize_public_response(response)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_order_by_id(order_id: RequiredId):
    """Retrieve the complete available order record by backend ID with internal configuration and personal data removed. Use get_order for normal support questions; use this tool only when backend-level detail is needed. The response remains read-only and backend-shaped."""
    return sanitize_public_response(await fetch_order_by_id(order_id))


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def search_orders(
    limit: Limit = 20,
    page: Page = 1,
    order_reference: Text = None,
    customer_name: Name = None,
    customer_email: Email = None,
    customer_phone: Phone = None,
    order_status: OrderStatus = None,
    start_date: Date = None,
    end_date: Date = None,
    payment_method: Text = None,
    channel_id: Identifier = None,
    seller_id: Identifier = None,
    warehouse_id: Identifier = None,
    customer_id: Identifier = None,
    channel_order_id: Identifier = None,
    timezone: Timezone = None,
):
    """Search orders and return compact summaries. Use this for customer, reference, status, or date searches; use get_orders for a general paginated list and get_order for one order. Name/reference matching follows the backend, email and phone are normalized, dates are YYYY-MM-DD in the selected timezone (default Asia/Kolkata), and limit is 1-100."""
    return sanitize_public_response(await find_orders(**locals()))


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_order(order_reference: Reference):
    """Retrieve compact order details for normal support and operations questions. Use get_order_by_id only for the complete raw record, and use search_orders when the order reference is not known. The lookup accepts an order number or channel order number."""
    return sanitize_public_response(await fetch_order(order_reference))


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_orders_summary(
    order_status: OrderStatus = None,
    start_date: Date = None,
    end_date: Date = None,
    page: Page = 1,
    limit: Limit = 100,
    timezone: Timezone = None,
):
    """Summarize read-only orders for an optional YYYY-MM-DD date range. Use this for totals and status aggregates, not order details; use search_orders for matching records and get_order for one order. Dates use the selected IANA timezone (default Asia/Kolkata), status is an enum, and limit is 1-100."""
    response = await fetch_orders_summary(
        order_status=order_status,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        page=page,
        limit=limit,
    )
    return sanitize_public_response(response)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def find_customer(
    name: Name = None,
    email: Email = None,
    phone: Phone = None,
    customer_id: Identifier = None,
    limit: Limit = 20,
    page: Page = 1,
):
    """Find customer candidates using read-only PointNXT order data. Use this first to identify a customer, then pass the returned record ID to get_customer_context for history and statistics. Name matching is search-based; email is trimmed/lowercased and phone compares normalized last-10-digit values; limit is 1-100."""
    return await find_customers(name, email, phone, customer_id, limit, page)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_customer_context(
    customer_id: Identifier = None,
    name: Name = None,
    email: Email = None,
    phone: Phone = None,
    limit: Limit = 20,
    page: Page = 1,
):
    """Get compact customer identity, paginated order history, and whole-history statistics. Use find_customer first when the customer ID is unknown; use search_orders for order-only questions. Customer contact filters are normalized, page is 1-based, and history totals may be marked partial at the scan cap."""
    return sanitize_public_response(
        await fetch_customer_context(customer_id, name, email, phone, limit, page)
    )


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_order_stats():
    """Retrieve read-only dashboard statistics grouped by PointNXT order status. Use this for aggregate status counts and get_orders or search_orders for the underlying records. It does not replace get_business_summary for a date-specific overview."""
    return await fetch_order_stats()


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_status_transitions():
    """Retrieve the backend's read-only workflow transitions for PointNXT order statuses. Use this when explaining which status changes are valid, not when searching orders. For records and counts, use get_orders or get_order_stats."""
    return await fetch_status_transitions()


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_products(
    limit: Limit = 10,
    page: Page = 1,
    sku: Text = None,
    name: Name = None,
    status: ProductStatus = None,
    barcode: Text = None,
    brand_id: Identifier = None,
    category_id: Identifier = None,
    channel_id: Identifier = None,
    vendor_id: Identifier = None,
    seller_id: Identifier = None,
    warehouse_id: Identifier = None,
):
    """Retrieve read-only catalog products with pagination and filters. Use this for a broad product list; use search_products for search and get_product for compact details of one product. SKU/barcode are exact backend filters, name follows backend search, status is ACTIVE, DRAFT, or ARCHIVED, and limit is 1-100."""
    response = await fetch_products(
        limit=limit,
        page=page,
        sku=sku,
        name=name,
        status=status,
        barcode=barcode,
        brand_id=brand_id,
        category_id=category_id,
        channel_id=channel_id,
        vendor_id=vendor_id,
        seller_id=seller_id,
        warehouse_id=warehouse_id,
    )
    return sanitize_public_response(response)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_product_by_id(product_id: RequiredId):
    """Retrieve available catalog detail by backend product ID with internal configuration and raw import data removed. Use get_product for normal compact product questions; use this tool only when additional backend fields are needed. The response is read-only."""
    return sanitize_public_response(await fetch_product_by_id(product_id))


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def search_products(
    limit: Limit = 20,
    page: Page = 1,
    name: Name = None,
    sku: Text = None,
    product_id: Identifier = None,
    status: ProductStatus = None,
    barcode: Text = None,
    brand_id: Identifier = None,
    category_id: Identifier = None,
    channel_id: Identifier = None,
    vendor_id: Identifier = None,
    seller_id: Identifier = None,
    warehouse_id: Identifier = None,
):
    """Search read-only products and return compact inventory-aware summaries. Use this for SKU, name, barcode, status, or ownership filters; use get_product for one product and get_product_summary for aggregate catalog metrics. SKU/barcode are exact backend filters, name is backend search, status is ACTIVE, DRAFT, or ARCHIVED, and limit is 1-100."""
    return await find_products(**locals())


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_product(product_id: RequiredId):
    """Retrieve compact details for one product by backend ID. Use search_products when the ID is unknown and get_product_by_id only when raw internal detail is required. Inventory is summarized by warehouse without bin-location detail."""
    return await fetch_product(product_id)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def search_inventory(
    limit: Limit = 20,
    page: Page = 1,
    sku: Text = None,
    name: Name = None,
    product_id: Identifier = None,
    status: ProductStatus = None,
    warehouse_id: Identifier = None,
):
    """Search read-only inventory attached to products. Use this for stock availability, search_products for product identity, and get_inventory_summary for aggregate totals. Status is ACTIVE, DRAFT, or ARCHIVED, the backend owns stock values, and limit is 1-100."""
    return await find_inventory(**locals())


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_inventory_summary():
    """Retrieve aggregate read-only inventory metrics from the backend. Use this for totals and use search_inventory for product-level stock; do not infer individual bins from this response. Values are backend-provided and not refreshed by this tool."""
    return await fetch_inventory_summary()


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def search_shipments(
    limit: Limit = 20,
    page: Page = 1,
    order_reference: Text = None,
    order_status: OrderStatus = None,
    warehouse_id: Identifier = None,
    channel_id: Identifier = None,
    include_without_shipments: Flag = False,
    ship_status: ShipStatus = None,
):
    """Find orders with stored shipments by default and return shipment/tracking summaries. Use this instead of search_orders for shipment questions; set include_without_shipments only when auditing all orders, or use ship_status READY_FOR_PICKUP, IN_TRANSIT, SHIPPED, DELIVERED, or CREATED. Results are read-only, tracking is not refreshed live, and scan-cap totals may be partial."""
    return await find_shipments(**locals())


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_order_shipments(order_reference: Reference):
    """Find stored shipment and tracking information for one order reference. Use search_shipments for a paginated shipment search and get_order for the compact order view. Tracking is stored backend data and is not refreshed live."""
    return await fetch_order_shipments(order_reference)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_fulfillment_queue(
    limit: Limit = 20,
    page: Page = 1,
    warehouse_id: Identifier = None,
):
    """Find read-only orders in the backend PENDING fulfillment queue. Use this for operational work lists, search_orders for general order filtering, and get_attention_queue for a cross-category summary. The backend does not expose separate picking/packing stages; limit is 1-100."""
    return await fetch_fulfillment_queue(limit, page, warehouse_id)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def search_returns(
    limit: Limit = 20,
    page: Page = 1,
    order_reference: Text = None,
    return_status: ReturnStatus = None,
    customer_id: Identifier = None,
    warehouse_id: Identifier = None,
):
    """Search read-only order records across six return-related statuses and return compact order-status-derived data. Use this for return queues and get_order_returns for one order; use search_orders for non-return status searches. An omitted return_status searches all six values, dates are not accepted here, and merged totals/pagination may scan multiple backend pages."""
    return await find_returns(**locals())


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_order_returns(order_reference: Reference):
    """Show read-only return information derived from order status for one order reference. Use search_returns for a paginated return queue and get_order for the normal compact order view. There is no dedicated return ID or reliable item-level return quantity/reason data in the current backend."""
    return await fetch_order_returns(order_reference)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_business_summary(date: Date = None, timezone: Timezone = None):
    """Summarize read-only backend-supported business indicators for one local calendar date. Use this for a compact daily overview, get_order_stats for status aggregates, and get_product_summary for catalog/inventory metrics. Date format is YYYY-MM-DD in the selected IANA timezone, default Asia/Kolkata; unavailable sections are reported rather than fabricated."""
    return await fetch_business_summary(date, timezone)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_attention_queue(date: Date = None, timezone: Timezone = None):
    """Return a read-only cross-category attention summary for one local calendar date. Use this for triage, then use get_fulfillment_queue, search_shipments, or search_returns for detailed records. Date format is YYYY-MM-DD in the selected IANA timezone, default Asia/Kolkata; samples are capped, oldest-first, and totals can be marked partial."""
    return await fetch_attention_queue(date, timezone)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
async def get_product_summary():
    """Retrieve aggregate read-only product and inventory dashboard metrics. Use this for catalog totals and inventory totals, and use search_products or search_inventory for product-level records. Its count basis is backend summary scope and may differ from paginated product-list totals."""
    return await fetch_product_summary()


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def check_backend_health():
    """Check PointNXT backend reachability, status, endpoint, and latency without exposing credentials. Use this for connectivity diagnostics, not business data or authentication state. A slow successful response is reported as degraded."""
    return run_backend_health_check()


@mcp.tool(annotations=READ_ONLY_LOCAL)
def check_authentication():
    """Check whether the current PointNXT authentication session is valid without returning tokens. Use this before business tools when a request may be unauthenticated. It reports status only and does not initiate login."""
    return run_authentication_check()


@mcp.tool(annotations=READ_ONLY_LOCAL)
def check_configuration():
    """Check whether required non-secret PointNXT and MCP configuration is present. Use this for local diagnostics before check_backend_health or authentication checks. Secret values are never returned."""
    return run_configuration_check()


@mcp.tool(annotations=READ_ONLY_LOCAL)
def get_mcp_metrics():
    """Return local in-process MCP request and backend performance metrics. Use this for operational diagnostics rather than customer or order data. Metrics contain aggregates and do not expose access or refresh tokens."""
    return get_metrics()


def _install_tool_error_boundary() -> None:
    """Expose safe validation/crash messages while logging unexpected tracebacks."""
    for tool in mcp._tool_manager.list_tools():
        original = tool.fn
        if inspect.iscoroutinefunction(original):

            @wraps(original)
            async def wrapped(*args, _original=original, _name=tool.name, **kwargs):
                try:
                    return await _original(*args, **kwargs)
                except ToolError:
                    raise
                except ValueError as error:
                    raise StructuredToolError("VALIDATION_ERROR", str(error)) from error
                except Exception as error:
                    logger.exception("Tool %s raised an unexpected exception", _name)
                    raise StructuredToolError(
                        "INTERNAL_ERROR", f"{_name} failed ({type(error).__name__})"
                    ) from error

        else:

            @wraps(original)
            def wrapped(*args, _original=original, _name=tool.name, **kwargs):
                try:
                    return _original(*args, **kwargs)
                except ToolError:
                    raise
                except ValueError as error:
                    raise StructuredToolError("VALIDATION_ERROR", str(error)) from error
                except Exception as error:
                    logger.exception("Tool %s raised an unexpected exception", _name)
                    raise StructuredToolError(
                        "INTERNAL_ERROR", f"{_name} failed ({type(error).__name__})"
                    ) from error

        tool.fn = wrapped


register_enabled_write_tools(mcp)
_install_tool_error_boundary()


if __name__ == "__main__":
    validate_mcp_oauth_configuration()
    validate_write_configuration()
    mcp.run()


def run_http(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the stateful HTTP server with the application protocol gate."""
    import anyio

    validate_mcp_oauth_configuration()
    anyio.run(_run_http, host, port)


async def _run_http(host: str, port: int) -> None:
    import uvicorn

    app = MCPProtocolGate(
        mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            host=host,
        )
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=MCP_FORWARDED_ALLOW_IPS,
    )
    await uvicorn.Server(config).serve()
