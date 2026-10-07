from mcp.server.mcpserver import MCPServer

from prompts import register_prompts
from services import session_store
from services.mcp_oauth import (
    authorize as oauth_authorize,
)
from services.mcp_oauth import (
    callback as oauth_callback,
)
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
from tools.auth import auth_callback, auth_check
from tools.auth import authenticate as exchange_session
from tools.auth import current_user as get_current_user
from tools.auth import login_with_credentials as login_credentials
from tools.auth import logout as clear_user_session
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
from tools.system import (
    check_authentication as run_authentication_check,
)
from tools.system import (
    check_backend_health as run_backend_health_check,
)
from tools.system import (
    check_configuration as run_configuration_check,
)

# Global MCP server object
mcp = MCPServer("PointNXT MCP", middleware=[MCPSessionMiddleware()])
register_prompts(mcp)


@mcp.custom_route("/auth/callback", methods=["GET"])
async def pointnxt_auth_callback(request):
    if session_store.has_oauth_transaction(request.query_params.get("state", "")):
        return await oauth_callback(request)
    return await auth_callback(request)


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


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token_route(request):
    return await oauth_token(request)


@mcp.custom_route("/oauth/revoke", methods=["POST"])
async def oauth_revoke_route(request):
    return await oauth_revoke(request)


@mcp.tool()
def current_user():
    """Return the currently authenticated PointNXT user."""
    return get_current_user()


@mcp.tool()
async def auth_status():
    """Return lightweight authentication status without exposing tokens."""
    user = get_current_user()
    return {
        "authenticated": user.get("authenticated", False),
        "user": user.get("user_id"),
        "email": user.get("email"),
        "tenant": user.get("tenant_id"),
        "expires_in_seconds": user.get("expires_in_seconds"),
    }


@mcp.tool(name="authenticate_with_pointnxt", title="Authenticate with PointNXT")
async def authenticate():
    """Open PointNXT in the browser and sign in without entering credentials here."""
    return await exchange_session()


@mcp.tool(name="login_with_credentials", title="Login to PointNXT")
async def login_with_credentials(
    email: str, password: str, remember_device: bool = True
):
    """Authenticate directly with PointNXT email and password."""
    return await login_credentials(email, password, remember_device)


@mcp.tool(name="logout_pointnxt", title="Logout from PointNXT")
async def logout_pointnxt():
    """Log out of the active PointNXT session."""
    return await clear_user_session()


@mcp.tool()
async def get_orders(
    limit: int = 10,
    page: int = 1,
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    payment_method: str | None = None,
    channel_id: str | None = None,
    seller_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    order_no: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    channel_order_id: str | None = None,
):
    """Retrieve PointNXT orders with pagination, status, and date filters.

    Examples: use order_status="CANCELLED" for cancelled orders, or provide
    start_date="2026-01-01" and end_date="2026-01-31" for a date range.
    """
    return await fetch_orders(
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
    )


@mcp.tool()
async def get_order_by_id(order_id: str):
    """Retrieve complete details for one PointNXT order by its ID."""
    return await fetch_order_by_id(order_id)


@mcp.tool()
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
):
    """Search orders and return compact LLM-friendly order summaries."""
    return await find_orders(**locals())


@mcp.tool()
async def get_order(order_reference: str):
    """Retrieve compact detailed information by display/reference number."""
    return await fetch_order(order_reference)


@mcp.tool()
async def get_orders_summary(
    order_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int = 1,
    limit: int = 100,
):
    """Summarize orders for a period using the existing order-list API."""
    return await fetch_orders_summary(
        order_status=order_status,
        start_date=start_date,
        end_date=end_date,
        page=page,
        limit=limit,
    )


@mcp.tool()
async def find_customer(
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    customer_id: str | None = None,
    limit: int = 20,
    page: int = 1,
):
    """Find customer candidates using supported PointNXT order data."""
    return await find_customers(name, email, phone, customer_id, limit, page)


@mcp.tool()
async def get_customer_context(
    customer_id: str | None = None,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    limit: int = 20,
    page: int = 1,
):
    """Get compact customer information, order history, and statistics."""
    return await fetch_customer_context(customer_id, name, email, phone, limit, page)


@mcp.tool()
async def get_order_stats():
    """Retrieve dashboard statistics grouped by PointNXT order status."""
    return await fetch_order_stats()


@mcp.tool()
async def get_status_transitions():
    """Retrieve valid workflow transitions for PointNXT order statuses."""
    return await fetch_status_transitions()


@mcp.tool()
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
):
    """Retrieve catalog products with pagination and business filters.

    Examples: use sku="SKU-123", status="ACTIVE", or provide category_id,
    vendor_id, warehouse_id, or channel_id to narrow the catalog results.
    """
    return await fetch_products(
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


@mcp.tool()
async def get_product_by_id(product_id: str):
    """Retrieve complete catalog information for one product by ID."""
    return await fetch_product_by_id(product_id)


@mcp.tool()
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
):
    """Search products with supported catalog filters."""
    return await find_products(**locals())


@mcp.tool()
async def get_product(product_id: str):
    """Retrieve compact details for one product."""
    return await fetch_product(product_id)


@mcp.tool()
async def search_inventory(
    limit: int = 20,
    page: int = 1,
    sku: str | None = None,
    name: str | None = None,
    product_id: str | None = None,
    status: str | None = None,
    warehouse_id: str | None = None,
):
    """Search backend-provided inventory attached to catalog products."""
    return await find_inventory(**locals())


@mcp.tool()
async def get_inventory_summary():
    """Retrieve backend-provided inventory summary metrics."""
    return await fetch_inventory_summary()


@mcp.tool()
async def search_shipments(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    order_status: str | None = None,
    warehouse_id: str | None = None,
    channel_id: str | None = None,
):
    """Find stored shipment information attached to orders."""
    return await find_shipments(**locals())


@mcp.tool()
async def get_order_shipments(order_reference: str):
    """Find stored shipment and tracking information for an order."""
    return await fetch_order_shipments(order_reference)


@mcp.tool()
async def get_fulfillment_queue(
    limit: int = 20,
    page: int = 1,
    warehouse_id: str | None = None,
):
    """Find pending fulfillment orders when detailed work queues are unavailable."""
    return await fetch_fulfillment_queue(limit, page, warehouse_id)


@mcp.tool()
async def search_returns(
    limit: int = 20,
    page: int = 1,
    order_reference: str | None = None,
    return_status: str | None = None,
    customer_id: str | None = None,
    warehouse_id: str | None = None,
):
    """Search return-related orders using backend-supported return statuses."""
    return await find_returns(**locals())


@mcp.tool()
async def get_order_returns(order_reference: str):
    """Show return records for an order without implying refund execution."""
    return await fetch_order_returns(order_reference)


@mcp.tool()
async def get_business_summary(date: str | None = None):
    """Summarize backend-supported business indicators for a UTC date."""
    return await fetch_business_summary(date)


@mcp.tool()
async def get_attention_queue(date: str | None = None):
    """Return evidence-backed business attention categories for a UTC date."""
    return await fetch_attention_queue(date)


@mcp.tool()
async def get_product_summary():
    """Retrieve catalog and inventory dashboard metrics for products."""
    return await fetch_product_summary()


@mcp.tool()
def check_backend_health():
    """Check PointNXT backend reachability and API latency."""
    return run_backend_health_check()


@mcp.tool()
def check_authentication():
    """Check whether PointNXT authentication is valid."""
    return run_authentication_check()


@mcp.tool()
def check_configuration():
    """Check whether required PointNXT configuration is present."""
    return run_configuration_check()


@mcp.tool()
def get_mcp_metrics():
    """Return in-process MCP request and backend performance metrics."""
    return get_metrics()


if __name__ == "__main__":
    mcp.run()


def run_http(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the stateful HTTP server with the application protocol gate."""
    import anyio

    anyio.run(_run_http, host, port)


async def _run_http(host: str, port: int) -> None:
    import uvicorn

    app = MCPProtocolGate(
        mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=False,
            host=host,
        )
    )
    config = uvicorn.Config(app, host=host, port=port)
    await uvicorn.Server(config).serve()
