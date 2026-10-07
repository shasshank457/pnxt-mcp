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
