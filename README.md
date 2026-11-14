# PointNXT MCP Server

PointNXT MCP is a Model Context Protocol server for retrieving PointNXT data and, when explicitly enabled, safely performing confirmed write operations.

## Architecture

```
AI assistant / MCP client
        |
        v
server.py (MCP tools)
        |
        v
tools/orders.py, tools/products.py, tools/system.py
        |
        v
services/pointnxt_api.py
        |
        v
PointNXT API
```

A request is received by an MCP tool, validated, converted into PointNXT query parameters, authenticated with a Bearer token and tenant ID, sent to PointNXT, and returned as JSON. Retries, token refresh, logs, and metrics are handled in the shared API client.

## Project structure

- `server.py`: MCP server and exposed tools.
- `config.py`: Loads environment configuration.
- `services/pointnxt_api.py`: Shared async/sync API client, authentication, refresh, retries, logging, and HTTP handling.
- `services/metrics.py`: Thread-safe in-process request metrics.
- `services/logger.py`: JSON logging helper.
- `tools/orders.py`: Order tools and validation.
- `tools/products.py`: Product tools.
- `tools/system.py`: Health and configuration tools.
- `Dockerfile`, `docker-compose.yml`: Container deployment.
- `.github/workflows/mcp-ci.yml`: GitHub Actions quality pipeline.

## MCP tools

### Orders

- `get_orders`: List orders with pagination and filters.
- `get_order_by_id`: Retrieve complete details for an internal order ID.
- `get_order_stats`: Retrieve order dashboard statistics.
- `get_status_transitions`: Retrieve valid workflow transitions.
- `get_orders_summary`: Summarize one retrieved order page.
- `add_order_note`: Preview and, after explicit confirmation, add an internal note to an order by internal UUID. First call it with `order_id` and `note`; then call it with `confirm=true` and the returned `confirmation_token`.
- `update_order_tags`: Preview and, after explicit confirmation, replace all tags on an order by internal UUID. First call it with `order_id` and the complete `tags` array; then call it with `confirm=true` and the returned `confirmation_token`. This is a replacement, not an append operation.
- `change_order_status`: Preview and, after explicit confirmation, change an order status using transitions fetched from PointNXT. The tool accepts `order_id`, `order_status`, and optional `notes`, `version`, and `cancel_reason`; backend state-machine rules remain authoritative.
- `update_order`: Preview and, after explicit confirmation, update approved fulfillment and measurement fields. The tool captures the current backend order version during preview and uses it on commit; status, notes, addresses, totals, currency, force, cancellation reason, warehouse, and model-provided version are excluded.
- `create_product`: Preview and, after explicit confirmation, create a product using approved catalog fields. Tenant identity is taken from the authenticated session and is never a model input; product creation does not create inventory or channel listings.
- `update_product`: Preview and, after explicit confirmation, update approved product fields after verifying the product belongs to the authenticated tenant. `tenantId`, identity, pricing, status, stock, ownership, and other excluded fields are never accepted or sent.
- `update_product_channel_listing`: Preview and, after explicit confirmation, update an existing product-channel listing after verifying product, channel, listing, and tenant ownership through authenticated verification reads. Price and MRP are excluded.
- `create_order`: Preview and, after explicit confirmation, create an order with validated addresses and line items. The backend calculates totals; payment is not processed, stock is not reserved, and tenant identity is never model-provided.

`get_orders` supports `limit`, `page`, `order_status`, `payment_method`, `channel_id`, `seller_id`, `warehouse_id`, `customer_id`, `start_date`, `end_date`, `order_no`, `customer_email`, `customer_phone`, and `channel_order_id`.

Dates use `YYYY-MM-DD`. Supported statuses include `PENDING`, `CONFIRMED`, `ON_HOLD`, `PARTIALLY_FULFILLED`, `FULFILLED`, `DELIVERED`, `CANCELLED`, `RETURN_REQUESTED`, `RETURN_RECEIVED`, `PARTIALLY_RETURNED`, `RETURNED`, `REFUNDED`, `PARTIALLY_REFUNDED`, `DISPUTED`, and `CLOSED`.

For a display number such as `#1466`, search with `order_no="1466"` first, then use the returned internal `id` with `get_order_by_id`.

### Products

- `get_products`: Search products with pagination, SKU, name, status, barcode, brand, category, channel, vendor, seller, and warehouse filters.
- `get_product_by_id`: Retrieve product details.
- `get_product_summary`: Retrieve catalog and inventory metrics.

### System

- `check_backend_health`: Backend reachability, status, and latency.
- `check_authentication`: Validates authentication through a lightweight request.
- `check_configuration`: Checks required configuration.
- `get_mcp_metrics`: Returns request and performance metrics.

## Configuration

Create `.env` locally and never commit it:

```env
POINTNXT_BASE_URL=http://localhost:3001/v1
POINTNXT_ACCESS_TOKEN=<jwt-access-token>
POINTNXT_REFRESH_TOKEN=<jwt-refresh-token>
POINTNXT_REFRESH_ENDPOINT=/auth/refresh
POINTNXT_TENANT_ID=<tenant-id>
```

### Optional write-tool framework

Write tools are disabled unless explicitly enabled. No business write tools are
currently registered; future write tools must be added through the shared
preview/commit framework in `services/write_tools.py`.

```env
PNXT_ENABLE_WRITE_TOOLS=false
PNXT_WRITE_TOOLS_ALLOWLIST=
PNXT_CONFIRMATION_SECRET=<at-least-32-byte-secret-when-enabled>
PNXT_CONFIRMATION_TTL_SECONDS=300
PNXT_WRITE_TIMEOUT_SECONDS=30
PNXT_WRITE_FREE_TEXT_MAX_LENGTH=1000
```

When enabled, `PNXT_CONFIRMATION_SECRET` is required and must be at least 32
bytes. Confirmation tokens are stateless HMAC-SHA256 tokens, while consumed
nonces and idempotency results use the framework's replaceable state-store
interface. The current MCP OAuth/session grants are already stored in the
SQLite database at `POINTNXT_SESSION_STORE_PATH`; deployments must keep that
path on the persistent Docker volume and keep `MCP_SESSION_ENCRYPTION_KEY`
stable across rebuilds.

To define a future business write, create a `WriteToolSpec` with the business
`load_target`, `validate`, `build_preview`, and `execute` callbacks, then pass
it to `register_enabled_write_tools`. The framework automatically adds
`confirm`, `confirmation_token`, and `idempotency_key`, performs target/version
checks, and maps errors into structured write responses.

The client sends `Authorization: Bearer <token>`, `x-tenant-id`, and JSON content headers.

## Authentication refresh

On `401 Unauthorized`, the client calls the configured refresh endpoint, sends the refresh token, accepts `accessToken` or `access_token`, updates the token in memory, and retries the original request once. Refresh failure returns a clear authentication error. Tokens are never logged.

## Retries

Timeouts, connection failures, and HTTP `502`, `503`, and `504` are retried. There are three total attempts with one-second and two-second delays. `400`, `401`, and `404` are not retried as temporary failures. A `401` can still trigger the separate one-time refresh flow.

## Async and concurrency

Orders and Products are async functions using `httpx.AsyncClient`:

```python
import asyncio
from tools.orders import get_orders
from tools.products import get_products

orders, products = await asyncio.gather(
    get_orders(limit=5),
    get_products(limit=5),
)
```

Timeouts, retries, authentication, and structured logging are preserved in the async path.

## Logs and metrics

Logs contain UTC timestamp, unique request ID, method, endpoint, tenant ID, status code, attempt, and latency. Errors and retries use separate levels. Credentials are not logged.

Metrics include total requests, successful requests, failed requests, average latency, average backend latency, and slow requests over one second. Metrics are process-local and reset when the server restarts.

## Local setup

Requires Python 3.12+, a reachable PointNXT API, and valid credentials.

```bash
python -m venv .venv
pip install -r requirements.txt
python server.py
```

For HTTP MCP transport on port 8000:

```bash
python -c "import server; server.run_http(host='0.0.0.0', port=8000)"
```

## Docker

```bash
docker compose up --build
```

Docker loads `.env`, exposes port 8000, restarts the service, and runs a health check.

If PointNXT runs on the host machine, use:

```env
POINTNXT_BASE_URL=http://host.docker.internal:3001/v1
```

Do not use `localhost` for a host API inside Docker. For another Compose service, use its service name.

## CI/CD and testing

GitHub Actions installs Python and dependencies, runs Ruff, compiles Python sources, and runs the Orders and Products tests. Configure these GitHub Secrets:

```
POINTNXT_BASE_URL
POINTNXT_ACCESS_TOKEN
POINTNXT_REFRESH_TOKEN
POINTNXT_REFRESH_ENDPOINT
POINTNXT_TENANT_ID
```

Run locally:

```bash
python test_orders.py
python test_products.py
python -m compileall -q .
```

Retrieval tests require a reachable backend.

## Security checklist

- Rotate exposed tokens immediately.
- Never commit `.env`.
- Use secret management in production.
- Use HTTPS for external MCP access.
- Restrict access to trusted clients.
- Use separate environment credentials.
- Do not log customer data or credentials.
- Monitor authentication and backend failures.

## Current limitations

Customer, shipment, and stock modules are placeholders. Product filter compatibility depends on the backend contract. Metrics are not persisted. External dashboards and distributed tracing are not configured. Per-client rate limiting and role-based authorization are not implemented. The order-note write is available only when the guarded write configuration is enabled.

## Troubleshooting

- **401:** Check access token, refresh token, refresh endpoint, and tenant ID.
- **400:** Check request parameters and backend API version.
- **Connection failure:** Check API availability, URL, firewall, and Docker networking.
- **Docker connectivity:** Use `host.docker.internal` for a host API.

