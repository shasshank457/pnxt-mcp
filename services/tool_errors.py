"""Structured MCP tool errors used by the shared server boundary."""

from mcp.server.mcpserver.exceptions import ToolError


class StructuredToolError(ToolError):
    """An anticipated tool error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")
