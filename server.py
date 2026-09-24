from mcp.server.mcpserver import MCPServer

from prompts import register_prompts
from services.metrics import get_metrics
from tools.auth import auth_callback, auth_check
from tools.auth import authenticate as exchange_session
from tools.auth import current_user as get_current_user
from tools.auth import login_with_credentials as login_credentials
from tools.auth import logout as clear_user_session
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
    get_status_transitions as fetch_status_transitions,
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
mcp = MCPServer("PointNXT MCP")
register_prompts(mcp)


@mcp.custom_route("/auth/callback", methods=["GET"])
async def pointnxt_auth_callback(request):
    return await auth_callback(request)


@mcp.custom_route("/auth/check", methods=["GET"])
async def pointnxt_auth_check(request):
    return await auth_check(request)


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
    )


@mcp.tool()
async def get_order_by_id(order_id: str):
    """Retrieve complete details for one PointNXT order by its ID."""
    return await fetch_order_by_id(order_id)


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
    limit: int | None = None,
    page: int | None = None,
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
