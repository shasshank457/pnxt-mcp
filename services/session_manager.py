"""Centralized in-process PointNXT authentication session manager."""

from datetime import datetime, timezone
from typing import Any

from services.auth_session import (
    AuthSession,
    clear_session,
    current_user,
    get_session,
    set_session,
)


def create_authenticated_session(
    payload: dict[str, Any], login_method: str
) -> AuthSession:
    data = payload.get("data", payload)
    user = data.get("user") or {}
    normalized = dict(data)
    normalized.update(
        {
            "userId": data.get("userId") or user.get("id"),
            "email": data.get("email") or user.get("email"),
            "name": data.get("name") or user.get("name"),
            "role": data.get("role") or user.get("role"),
        }
    )
    session = set_session(normalized)
    session.login_method = login_method
    session.name = normalized.get("name")
    session.role = normalized.get("role")
    session.created_at = datetime.now(timezone.utc)
    session.last_used_at = session.created_at
    return session


class SessionManager:
    create_session = staticmethod(create_authenticated_session)
    get_session = staticmethod(get_session)
    clear_session = staticmethod(clear_session)
    current_user = staticmethod(current_user)

    @staticmethod
    def is_authenticated() -> bool:
        session = get_session()
        return bool(session and session.is_authenticated())

    @staticmethod
    def is_expired() -> bool:
        session = get_session()
        return not session or session.is_expired()
