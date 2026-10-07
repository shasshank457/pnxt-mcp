"""Bind each MCP request to its transport connection session."""
from typing import Any

from mcp.server.context import (
    CallNext,
    HandlerResult,
    ServerMiddleware,
    ServerRequestContext,
)

from services.mcp_oauth import validate_bearer
from services.request_context import reset_request_key, set_request_key


class MCPSessionMiddleware(ServerMiddleware[Any]):
    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        connection = getattr(ctx.session, "_connection", None)
        session_id = getattr(connection, "session_id", None)
        # The 2026-07-28 Streamable HTTP discovery probe is deliberately
        # sessionless.  It only negotiates protocol capabilities; it cannot
        # invoke business tools or access PointNXT data.  Allow that probe so
        # clients can fall back to the stateful initialize handshake.  Every
        # other HTTP MCP request still requires a stable transport identity.
        if (
            ctx.request is not None
            and getattr(ctx, "method", None) == "server/discover"
            and getattr(ctx, "protocol_version", None) == "2026-07-28"
        ):
            return await call_next(ctx)
        # A missing HTTP identity must fail closed.  Only an actual non-HTTP
        # stdio request may use the process-local stdio identity.
        if ctx.request is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                raise RuntimeError("No stable MCP session identity is available")
            key = session_id.strip()
        else:
            key = session_id or "stdio"
        if ctx.request is not None:
            authorization = getattr(ctx.request, "headers", {}).get("authorization", "")
            if authorization.lower().startswith("bearer ") and not validate_bearer(
                authorization[7:].strip(), str(key)
            ):
                raise RuntimeError("Invalid MCP access token")
        token = set_request_key(str(key))
        try:
            return await call_next(ctx)
        finally:
            reset_request_key(token)
