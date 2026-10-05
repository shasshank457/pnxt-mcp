"""Session store keyed by MCP connection identity."""
from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.auth_session import AuthSession

_sessions: dict[str, AuthSession] = {}
_lock = RLock()

def get(key: str | None) -> AuthSession | None:
    return _sessions.get(key) if key else None

def set_(key: str | None, session: AuthSession) -> AuthSession:
    if not key:
        raise RuntimeError("No MCP session context is available for authentication")
    with _lock:
        _sessions[key] = session
    return session

def clear(key: str | None) -> None:
    if key:
        with _lock:
            _sessions.pop(key, None)

def clear_all_for_tests() -> None:
    with _lock:
        _sessions.clear()
