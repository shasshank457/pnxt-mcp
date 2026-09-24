"""In-process PointNXT browser-session state."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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


_session: AuthSession | None = None


def get_session() -> AuthSession | None:
    return _session


def clear_session() -> None:
    global _session
    _session = None


def set_session(payload: dict[str, Any]) -> AuthSession:
    data = payload.get("data", payload)
    expiry = data.get("expiresAt") or data.get("expires_at") or data.get("expiresIn")
    if isinstance(expiry, (int, float)):
        parsed = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + expiry, timezone.utc
        )
    else:
        parsed = (
            datetime.fromisoformat(expiry.replace("Z", "+00:00")) if expiry else None
        )
    global _session
    _session = AuthSession(
        data.get("accessToken") or data.get("access_token") or "",
        data.get("refreshToken") or data.get("refresh_token") or "",
        data.get("tenantId") or data.get("tenant_id") or "",
        data.get("membershipId") or data.get("membership_id"),
        data.get("userId") or data.get("user_id"),
        data.get("email"),
        parsed,
    )
    return _session


def current_user() -> dict[str, Any]:
    return (
        _session.as_dict()
        if _session
        else {
            "authenticated": False,
            "email": None,
            "user_id": None,
            "tenant_id": None,
            "membership_id": None,
            "session_expiry": None,
            "expires_in_seconds": None,
        }
    )
