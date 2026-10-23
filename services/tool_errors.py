"""Structured MCP tool errors used by the shared server boundary."""

from mcp.server.mcpserver.exceptions import ToolError


class StructuredToolError(ToolError):
    """An anticipated tool error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")


def unsupported_operation(capability: str, *, action: str, details: dict | None = None) -> StructuredToolError:
    """Build a standard fail-closed error for an absent backend capability."""
    merged = {"action": action, "write_performed": False}
    if details:
        merged.update(details)
    return StructuredToolError(
        "UNSUPPORTED_OPERATION",
        f"PointNXT does not expose {capability}; no write was performed.",
        details=merged,
    )
