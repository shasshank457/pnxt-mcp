"""Bind each MCP request to its transport connection session."""
from typing import Any
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from services.request_context import reset_request_key, set_request_key

class MCPSessionMiddleware(ServerMiddleware[Any]):
    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        connection = getattr(ctx.session, "_connection", None)
        key = getattr(connection, "session_id", None) or "stdio"
        token = set_request_key(str(key))
        try:
            return await call_next(ctx)
        finally:
            reset_request_key(token)
