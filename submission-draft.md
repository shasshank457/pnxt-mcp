# PointNXT OpenAI Submission Draft

Status: local planning draft only. This file is not a submission package and does not establish portal verification, production connectivity, legal approval, or review readiness.

The local manifests and review cases are drafts only. They have not been submitted to or verified through the OpenAI portal. The ZIP package has not been created, and public submission is not yet ready.

## Submission identity

- Display name: `PointNXT MCP`
- Publisher: `PointNXT`
- Website: https://pointnxt.com
- Production MCP endpoint: https://mcp.pointnxt.com/mcp
- Submission model: remote MCP server, universal endpoint
- Local test status supplied by publisher: `7 passed` using `pytest -q -p no:cacheprovider`

## Completed local preparation

- `plugin.json` exists at the repository root and passed official Agent Plugins JSON Schema validation.
- `mcp.json` exists at the repository root and passed official Agent Plugins MCP JSON Schema validation.
- `plugin.json` and `mcp.json` consistently identify PointNXT MCP and `https://mcp.pointnxt.com/mcp`.
- `submission-test-cases.md` exists and contains exactly five positive cases (`P01`–`P05`) and three negative cases (`N01`–`N03`).
- The local test suite passed 7 tests using `pytest -q -p no:cacheprovider`.
- No ZIP package has been created.

## Metadata draft

### Short description

`PointNXT commerce data`

Character count: 23. The final portal limit must be rechecked during submission.

### Detailed description

PointNXT MCP provides authenticated, tenant-scoped access to PointNXT commerce data through an AI assistant. It can retrieve and search orders, order details, order statistics, valid status transitions, products, product details, product and inventory summaries, shipment information attached to orders, return-related order information, customer context derived from supported order data, and evidence-backed business summaries and attention queues. It also exposes backend health, authentication, configuration, and in-process metrics diagnostics. Commerce operations are read-only: the server does not execute order changes, refunds, or other commerce writes. Authentication operations establish, refresh, or clear the connected PointNXT session and may contact the PointNXT service. Results and access are limited by the authenticated PointNXT account, tenant context, and backend authorization.

### Starter prompts

1. `Show today's orders and totals`
2. `Find pending orders this week`
3. `Summarize the product catalog`

These prompts correspond to registered capabilities including order retrieval/search and product summary tools. They are draft portal starter prompts, not MCP prompt registrations.

### Initial release notes

Initial public submission draft for PointNXT MCP. Provides authenticated, tenant-scoped read access to PointNXT orders, products, inventory summaries, customer context, shipments, returns, business summaries, and diagnostics through a remote MCP endpoint. Commerce write operations are not included.

## Repository evidence used

- Server registration and MCP endpoint path: `server.py`, including the Streamable HTTP path `/mcp`.
- Public base URL configuration: `config.py` (`MCP_PUBLIC_BASE_URL` defaults to `https://mcp.pointnxt.com`).
- Tool implementations and registration: `server.py`, `tools/`, and `services/`.
- Existing capability and limitation summary: `README.md` and `generated_context.txt`.
- Registered reusable MCP prompts: `prompts.py`.

The repository now contains root `plugin.json`, root `mcp.json`, this draft, and `submission-test-cases.md`. No `.codex-plugin/plugin.json`, `.mcp.json`, logo asset, privacy URL, terms URL, support URL, demo recording URL, or portal verification result is present. The review cases are documented locally in Markdown and have not yet been imported into or verified by the OpenAI portal.

## Pending with the PointNXT team

- Privacy policy URL: pending; no policy page exists yet.
- Terms of service URL: pending; no terms page exists yet.
- Support URL: pending; no support URL is supplied. Do not invent one.
- Logo/icon: pending; no logo asset was supplied.
- Demo recording URL: pending; no recording is supplied.
- Reviewer account and approved sample data: pending; provide through the secure portal review form, not a package.

## Pending OpenAI portal verification

- Domain-verification challenge: pending; obtain the token from the OpenAI portal and host it at the required challenge path.
- Current OpenAI MCP tool scan: pending.
- Live OAuth verification: pending.
- Verified PointNXT publisher identity/business verification: pending portal verification.
- Portal policy attestations, country availability, and any required localization: pending portal completion.

The five positive and three negative review cases are prepared locally, but each must still be run and confirmed with the dedicated reviewer account and supplied sample data before submission.

## Privacy-policy preparation checklist

The publisher should address, based on repository behavior and without treating this as legal advice:

- What account and authentication data is collected or processed, including email, user ID, membership ID, tenant ID, access/refresh tokens, and session expiry data.
- What PointNXT business data may be returned, including orders, customer details, products, inventory fields, shipments, returns, and business summaries.
- Tenant scoping and the role of the authenticated PointNXT account in determining accessible data.
- Authentication flows, browser callbacks, OAuth authorization-code/PKCE handling, token refresh, logout, and revocation.
- Logging and observability: request IDs, endpoints, status, latency, tenant metadata, metrics, and the repository’s stated fingerprinting of sensitive parameters.
- Retention and deletion of session records, OAuth transactions, OAuth codes, and MCP tokens in the deployed environment.
- Third-party processing by PointNXT and any infrastructure providers.
- User rights, contact method, incident handling, and data-transfer or regional-processing disclosures applicable to the publisher.

## Terms-of-service preparation checklist

- Authorized use of the PointNXT MCP service and connected PointNXT accounts.
- User responsibility for account permissions, tenant access, and instructions submitted to the assistant.
- Service availability, rate limits, backend dependency, and limitations of derived customer/shipment/return views.
- Clear statement that read-only commerce tools do not execute refunds, order changes, or other commerce writes.
- Accuracy limitations for backend-provided inventory and business metrics.
- Prohibited misuse, unauthorized tenant access, credential sharing, and attempts to bypass access controls.
- Intellectual-property, confidentiality, liability, suspension, termination, and governing-law provisions reviewed by the publisher.
- Support, security-reporting, and change-notification contact details.

## Official format reference

OpenAI's current documentation describes a portable plugin package with root `plugin.json` and root `mcp.json` for the remote MCP configuration; a remote HTTP MCP declaration uses the `streamable-http` transport and the public HTTPS MCP URL. OpenAI's submission flow separately connects and scans the remote MCP server through the portal. The local manifests are schema-valid drafts, but the package remains incomplete until the pending legal, asset, reviewer, OAuth, and portal-verification requirements are supplied.

References:

- https://developers.openai.com/plugins/build/plugins
- https://developers.openai.com/plugins/deploy/submission
- https://developers.openai.com/plugins/deploy/submission-errors
- https://developers.openai.com/plugins/deploy/app-review

## Unresolved questions

- Which final privacy, terms, and support pages will PointNXT publish?
- Which PointNXT demo tenant and permissions should reviewers use?
- Which stable sample data will be used to demonstrate the prepared five positive and three negative scenarios?
- Which logo asset and brand colors should represent PointNXT in the directory?
- Will the portal accept the current production OAuth flow without additional configuration?
