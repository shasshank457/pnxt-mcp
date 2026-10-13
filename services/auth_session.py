"""MCP-connection-scoped PointNXT browser-session state."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from services import session_store
from services.request_context import get_request_key


@dataclass
class AuthSession:
    access_token: str
    refresh_token: str
    tenant_id: str
    membership_id: str | None = None
    user_id: str | None = None
    email: str | None = None
    expires_at: datetime | None = None
    login_method: str | None = None
    name: str | None = None
    role: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None

    def is_authenticated(self) -> bool:
        return bool(self.access_token and self.tenant_id) and (
            self.expires_at is None or self.expires_at > datetime.now(timezone.utc)
        )

    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(
            timezone.utc
        )

    def expires_in_seconds(self) -> int | None:
        if self.expires_at is None:
            return None
        return max(
            0, int((self.expires_at - datetime.now(timezone.utc)).total_seconds())
        )

    def get_access_token(self) -> str:
        return self.access_token

    def get_refresh_token(self) -> str:
        return self.refresh_token

    def get_tenant_id(self) -> str:
        return self.tenant_id

    def clear_session(self) -> None:
        self.access_token = self.refresh_token = self.tenant_id = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.is_authenticated(),
            "email": self.email,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "membership_id": self.membership_id,
            "name": self.name,
            "role": self.role,
            "login_method": self.login_method,
            "session_expiry": self.expires_at.isoformat() if self.expires_at else None,
            "expires_in_seconds": self.expires_in_seconds(),
        }


def get_session() -> AuthSession | None:
    return session_store.get(get_request_key())


def clear_session() -> None:
    session_store.clear(get_request_key())


def invalidate_session() -> None:
    """Revoke MCP grants before clearing an invalidated auth session.

    Grant revocation is deliberately idempotent.  The durable session is
    cleared in a finally block so an unexpected storage failure cannot leave
    the backend authentication session usable.
    """
    request_key = get_request_key()
    try:
        session_store.revoke_mcp_grants(request_key)
    finally:
        session_store.clear(request_key)


def _parse_expiry(data: dict[str, Any]) -> datetime | None:
    """Parse confirmed expiry fields without guessing ambiguous semantics."""
    if "expiresIn" in data and data["expiresIn"] is not None:
        value = data["expiresIn"]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + value, timezone.utc
            )
        return None
    value = data.get("expiresAt", data.get("expires_at"))
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def set_session(payload: dict[str, Any]) -> AuthSession:
    data = payload.get("data", payload)
    user = data.get("user") or {}
    parsed = _parse_expiry(data)
    session = AuthSession(
        data.get("accessToken") or data.get("access_token") or "",
        data.get("refreshToken") or data.get("refresh_token") or "",
        data.get("tenantId") or data.get("tenant_id") or "",
        data.get("membershipId") or data.get("membership_id"),
        data.get("userId") or data.get("user_id"),
        data.get("email") or user.get("email"),
        parsed,
        data.get("loginMethod") or data.get("login_method"),
        data.get("name") or user.get("name"),
        data.get("role") or user.get("role"),
    )
    return session_store.set_(get_request_key(), session)


def current_user() -> dict[str, Any]:
    return (
        get_session().as_dict()
        if get_session()
        else {
            "authenticated": False,
            "email": None,
            "user_id": None,
            "tenant_id": None,
            "membership_id": None,
            "name": None,
            "role": None,
            "login_method": None,
            "session_expiry": None,
            "expires_in_seconds": None,
        }
    )
