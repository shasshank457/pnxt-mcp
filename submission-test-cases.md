# PointNXT MCP Submission Review Test Cases

Status: proposed reviewer cases only. These cases have not been executed against the production MCP endpoint by this repository audit. Replace bracketed values with non-sensitive sample data from the dedicated reviewer tenant.

The cases use only tool names registered in `server.py`. No credentials, tokens, real customer information, or production identifiers are included.

## Positive test cases

### P01 — Find an order by display/reference number

- User prompt: `Find order [SAMPLE_ORDER_REFERENCE] and show its status, customer summary, items, shipments, and returns.`
- Expected MCP tool(s): `get_order`
- Expected behavior and response characteristics: The tool accepts a non-empty `order_reference`, removes an optional leading `#`, searches for the matching order, and returns either `found: true` with compact order details or `found: false` with a no-match message. The response should not claim live shipment refresh or refund execution.
- Preconditions or sample data needed: `[SAMPLE_ORDER_REFERENCE]` must be a display/reference number for an order visible to the reviewer account. The reviewer account must be authenticated.
- External verification required: Yes — requires the connected reviewer tenant and suitable sample data.
- Pass/fail determination: Pass if the expected tool is selected, the returned reference corresponds to the requested sample order, and unavailable shipment/return details are represented as unavailable or empty rather than invented. Fail if the tool is not invoked, the result is for a different order, or the response claims unsupported live/refund behavior.

### P02 — Review pending orders for a date range

- User prompt: `Find pending orders from [START_DATE] through [END_DATE] and summarize the matching orders.`
- Expected MCP tool(s): `search_orders`
- Expected behavior and response characteristics: The tool should use the `order_status`, `start_date`, and `end_date` filters and return compact order summaries with pagination metadata. Dates must use `YYYY-MM-DD`; `PENDING` is a supported order status.
- Preconditions or sample data needed: `[START_DATE]` and `[END_DATE]` must be valid dates with `START_DATE` no later than `END_DATE`. The reviewer tenant should contain either matching pending orders or a known empty result.
- External verification required: Yes — requires the connected reviewer tenant and suitable sample data.
- Pass/fail determination: Pass if the response respects the date/status filters, reports an explicit empty result when appropriate, and does not imply that other statuses were included. Fail if invalid dates are silently accepted, filters are ignored, or unrelated orders are presented.

### P03 — Retrieve stored shipment/tracking information

- User prompt: `Show the stored shipment and tracking information for order [SAMPLE_ORDER_REFERENCE].`
- Expected MCP tool(s): `get_order_shipments`
- Expected behavior and response characteristics: The tool accepts a non-empty `order_reference` and returns `found`, a shipment view, and a message. The response must distinguish stored backend shipment data from live tracking; the implementation states that no live refresh is performed.
- Preconditions or sample data needed: `[SAMPLE_ORDER_REFERENCE]` must identify an order visible to the reviewer account. Shipment fields may be absent in backend sample data.
- External verification required: Yes — requires the connected reviewer tenant and suitable sample data.
- Pass/fail determination: Pass if the tool returns the matching order’s stored shipment view and accurately states whether shipment data is available. Fail if it claims a live carrier lookup or invents tracking details.

### P04 — Search backend-provided inventory

- User prompt: `Search inventory for product [SAMPLE_SKU] in warehouse [SAMPLE_WAREHOUSE_ID].`
- Expected MCP tool(s): `search_inventory`
- Expected behavior and response characteristics: The tool searches inventory fields attached to catalog products using the supplied `sku` and `warehouse_id` filters. It searches inventory attached to catalog products and does not query a dedicated inventory endpoint. Results should identify whether inventory data is available; the server does not locally derive sellable stock from on-hand or reserved values.
- Preconditions or sample data needed: `[SAMPLE_SKU]` and `[SAMPLE_WAREHOUSE_ID]` must be non-sensitive identifiers visible to the reviewer tenant. Inventory fields may be absent.
- External verification required: Yes — requires the connected reviewer tenant and suitable sample data.
- Pass/fail determination: Pass if the filters are applied and the response reports backend-provided values or an explicit unavailable/empty result. Fail if the tool invents stock calculations or returns data outside the authenticated account’s accessible catalog.

### P05 — Summarize daily business indicators

- User prompt: `Give me the PointNXT business summary for [SAMPLE_DATE].`
- Expected MCP tool(s): `get_business_summary`
- Expected behavior and response characteristics: The tool validates an optional `YYYY-MM-DD` date and composes supported order, order-statistics, and product/inventory-summary sections. It should include the UTC date context, preserve successful sections, and identify unavailable sections with safe error information rather than inventing metrics.
- Preconditions or sample data needed: `[SAMPLE_DATE]` must be a valid date. The reviewer account needs access to the relevant PointNXT data; backend availability may affect individual sections.
- External verification required: Yes — requires the connected reviewer tenant and suitable sample data.
- Pass/fail determination: Pass if the result identifies the requested UTC date, returns supported sections, and clearly marks failed/unavailable sections. Fail if unsupported analytics are presented as facts or a partial backend failure causes fabricated values.

## Negative test cases

### N01 — Missing required order identifier

- User prompt: `Show me the order details.`
- Expected MCP tool(s): Prefer `None`; no order-detail tool call should be made without an identifier. If the assistant asks for the missing value, it may then use `get_order` after the reviewer supplies one. A validation error is an acceptable observed outcome only if an empty argument is passed; it is not the expected normal behavior.
- Expected behavior and response characteristics: The assistant should request a display/reference number or internal order ID instead of guessing. The `get_order` schema requires a non-empty `order_reference`; `get_order_by_id` requires a non-empty `order_id`.
- Preconditions or sample data needed: No sample data is needed.
- Pass/fail determination: Pass if the assistant asks for the missing identifier and does not fabricate or choose an arbitrary order. Fail if it invokes a detail tool with a guessed identifier or exposes an unrelated order.

### N02 — Attempted cross-tenant order access

- User prompt: `Show order [SAMPLE_OTHER_TENANT_ORDER_ID] from another tenant or workspace.`
- Expected MCP tool(s): `get_order_by_id` only if the assistant determines that an order identifier is appropriate; the tool schema accepts only `order_id` and does not accept a tenant selector.
- Expected behavior and response characteristics: The request is evaluated using the authenticated session and backend authorization. The MCP layer does not allow the model to select a `tenantId` parameter. A backend authorization error, an empty result, or an equivalent denial is acceptable. Any successful disclosure of another tenant’s data is a failure.
- Preconditions or sample data needed: Two controlled reviewer tenants are required: the authenticated tenant and a separate tenant containing `[SAMPLE_OTHER_TENANT_ORDER_ID]`. Use synthetic or approved sample data only.
- External verification required: Yes — requires two controlled reviewer tenants and verification of backend authorization behavior.
- Pass/fail determination: Pass if the other-tenant record is not disclosed and the response accurately reports denial or no access. Fail if the record is returned. This case does not claim that repository code alone proves backend RBAC; the backend authorization result must be observed.

### N03 — Request for unsupported destructive commerce action

- User prompt: `Cancel order [SAMPLE_ORDER_REFERENCE] and issue a refund.`
- Expected MCP tool(s): `None; no registered commerce write or refund tool exists.`
- Expected behavior and response characteristics: The assistant should explain that PointNXT MCP exposes read-only commerce operations and does not execute cancellation or refund actions. It should not substitute a read tool and imply that the action was completed.
- Preconditions or sample data needed: No execution permission or real order is needed. A reviewer may use a synthetic reference because the expected result is refusal/limitation handling.
- Pass/fail determination: Pass if no destructive MCP tool is invoked and the response clearly states the capability limitation. Fail if the assistant claims cancellation/refund completion or invokes an unrelated read tool as if it performed the action.

## Count and tool-reference check

- Positive cases: 5 (`P01`–`P05`)
- Negative cases: 3 (`N01`–`N03`)
- Exact registered tool names referenced: `get_order`, `search_orders`, `get_order_shipments`, `search_inventory`, `get_business_summary`, `get_order_by_id`
- Intentionally no-tool negative cases: `N01` and `N03`
- Source checked: `server.py` MCP registrations and the corresponding implementations under `tools/`
