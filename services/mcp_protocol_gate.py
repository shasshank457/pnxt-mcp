"""Reject unsupported modern MCP protocol traffic before SDK dispatch."""

from collections.abc import Iterable

from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS, MCP_PROTOCOL_VERSION_HEADER
from mcp_types import ErrorData, JSONRPCError, UnsupportedProtocolVersionErrorData
from mcp_types.jsonrpc import UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class MCPProtocolGate:
    """Keep the application on the handshake-era, stateful MCP transport."""

    def __init__(self, app: ASGIApp, *, mcp_path: str = "/mcp") -> None:
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.mcp_path:
            await self.app(scope, receive, send)
            return

        protocol_version = _header_value(scope.get("headers", ()), MCP_PROTOCOL_VERSION_HEADER)
        if protocol_version is None or protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
            await self.app(scope, receive, send)
            return

        rejection = JSONRPCError(
            jsonrpc="2.0",
            id=None,
            error=ErrorData(
                code=UNSUPPORTED_PROTOCOL_VERSION,
                message="Unsupported protocol version",
                data=UnsupportedProtocolVersionErrorData(
                    supported=list(HANDSHAKE_PROTOCOL_VERSIONS),
                    requested=protocol_version,
                ).model_dump(mode="json"),
            ),
        )
        response = JSONResponse(
            rejection.model_dump(mode="json", by_alias=True, exclude_none=True),
            status_code=ERROR_CODE_HTTP_STATUS[UNSUPPORTED_PROTOCOL_VERSION],
        )
        await response(scope, receive, send)


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for header_name, header_value in headers:
        if header_name.lower() == wanted:
            return header_value.decode("latin-1")
    return None
