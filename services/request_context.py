"""Request/session identity binding for MCP transport contexts."""
from contextvars import ContextVar

_request_key: ContextVar[str | None] = ContextVar("pointnxt_request_key", default=None)

def get_request_key() -> str | None:
    return _request_key.get()

def set_request_key(key: str | None):
    return _request_key.set(key)

def reset_request_key(token) -> None:
    _request_key.reset(token)
