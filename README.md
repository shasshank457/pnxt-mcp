# PointNXT MCP Server

PointNXT MCP is a read-only Model Context Protocol server for retrieving PointNXT orders, products, health information, and metrics through an AI assistant.

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

Customer, shipment, and stock modules are placeholders. Product filter compatibility depends on the backend contract. Metrics are not persisted. External dashboards and distributed tracing are not configured. Per-client rate limiting and role-based authorization are not implemented. Write operations are intentionally absent.

## Troubleshooting

- **401:** Check access token, refresh token, refresh endpoint, and tenant ID.
- **400:** Check request parameters and backend API version.
- **Connection failure:** Check API availability, URL, firewall, and Docker networking.
- **Docker connectivity:** Use `host.docker.internal` for a host API.

