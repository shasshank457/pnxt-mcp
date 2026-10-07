"""Bind each MCP request to its transport connection session."""
import logging
from typing import Any

from mcp.server.context import (
    CallNext,
    HandlerResult,
    ServerMiddleware,
    ServerRequestContext,
)

from services.mcp_oauth import validate_bearer
from services.request_context import reset_request_key, set_request_key

logger = logging.getLogger(__name__)


class MCPSessionMiddleware(ServerMiddleware[Any]):
    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        connection = getattr(ctx.session, "_connection", None)
        session_id = getattr(connection, "session_id", None)
        request = ctx.request
        request_headers = getattr(request, "headers", {}) if request is not None else {}
        method = getattr(ctx, "method", None)
        try:
            protocol_version = request_headers.get("mcp-protocol-version")
            request_session_header_present = bool(request_headers.get("mcp-session-id"))
            logger.debug(
                "MCP request method=%s protocol_version=%s request_present=%s "
                "connection_session_id_present=%s request_session_header_present=%s",
                method,
                protocol_version,
                request is not None,
                bool(session_id),
                request_session_header_present,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never mask MCP errors
            protocol_version = None
            request_session_header_present = None
        # The 2026-07-28 Streamable HTTP discovery probe is deliberately
        # sessionless.  It only negotiates protocol capabilities; it cannot
        # invoke business tools or access PointNXT data.  Allow that probe so
        # clients can fall back to the stateful initialize handshake.  Every
        # other HTTP MCP request still requires a stable transport identity.
        if (
            request is not None
            and getattr(ctx, "method", None) == "server/discover"
            and getattr(ctx, "protocol_version", None) == "2026-07-28"
        ):
            result = None
            try:
                result = await call_next(ctx)
                return result
            finally:
                _log_completion(method, result)
        # A missing HTTP identity must fail closed.  Only an actual non-HTTP
        # stdio request may use the process-local stdio identity.
        if request is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                _log_completion(method, None)
                raise RuntimeError("No stable MCP session identity is available")
            key = session_id.strip()
        else:
            key = session_id or "stdio"
        if request is not None:
            authorization = request_headers.get("authorization", "")
            if authorization.lower().startswith("bearer ") and not validate_bearer(
                authorization[7:].strip(), str(key)
            ):
                _log_completion(method, None)
                raise RuntimeError("Invalid MCP access token")
        token = set_request_key(str(key))
        result = None
        try:
            result = await call_next(ctx)
            return result
        finally:
            reset_request_key(token)
            _log_completion(method, result)


def _log_completion(method: str | None, result: HandlerResult | None) -> None:
    try:
        response_status = getattr(result, "status_code", None)
        response_headers = getattr(result, "headers", None)
        if response_headers is not None:
            response_session_header_present = bool(response_headers.get("mcp-session-id"))
            response_protocol_version = response_headers.get("mcp-protocol-version")
        else:
            response_session_header_present = None
            response_protocol_version = None
        logger.debug(
            "MCP request completed method=%s response_status=%s "
            "response_session_header_present=%s response_protocol_version=%s",
            method,
            response_status,
            response_session_header_present,
            response_protocol_version,
        )
    except Exception:  # noqa: BLE001 - diagnostics must never mask MCP errors
        return
