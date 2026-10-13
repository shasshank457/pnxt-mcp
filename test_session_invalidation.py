"""Regression tests for MCP grant revocation during auth-session invalidation."""

import pytest
from types import SimpleNamespace

from services import session_store
from services.auth_session import AuthSession, invalidate_session
from services.mcp_oauth import MCP_RESOURCE, _hash
from services.request_context import reset_request_key, set_request_key
from tools import auth


def _create_grants(key: str, user: str, tenant: str, membership: str):
    session_payload = {
        "auth_session_key": key,
        "scope": "mcp",
        "resource": MCP_RESOURCE,
        "user_id": user,
        "tenant_id": tenant,
        "membership_id": membership,
    }
    session_store.put_mcp_token(
        _hash(f"access-{key}"), session_payload, 4_000_000_000
    )
    refresh_payload = {
        **session_payload,
        "client_id": "claude",
    }
    session_store.put_mcp_refresh_token(
        _hash(f"refresh-{key}"), refresh_payload, 4_000_000_000
    )


@pytest.fixture(autouse=True)
def clean_store():
    session_store.clear_all_for_tests()
    yield
    session_store.clear_all_for_tests()


def test_invalidation_revokes_all_grants_but_preserves_unrelated_session(monkeypatch):
    _create_grants("session-a", "user-session-a", "tenant-a", "membership-session-a")
    _create_grants("session-b", "user-session-b", "tenant-b", "membership-session-b")
    sessions = {
        "session-b": AuthSession(
            access_token="access",
            refresh_token="refresh",
            tenant_id="tenant-b",
            user_id="user-session-b",
            membership_id="membership-session-b",
        )
    }
    monkeypatch.setattr(session_store, "get", lambda key: sessions.get(key))

    token = set_request_key("session-a")
    try:
        invalidate_session()
    finally:
        reset_request_key(token)

    assert session_store.get_mcp_refresh_token(_hash("refresh-session-a")) is None
    assert session_store.get_mcp_token(_hash("access-session-a")) is None

    assert session_store.get("session-b") is not None
    assert session_store.get_mcp_refresh_token(_hash("refresh-session-b")) is not None
    assert session_store.get_mcp_token(_hash("access-session-b")) is not None


def test_repeated_invalidation_is_safe():
    _create_grants("session-a", "user-session-a", "tenant-a", "membership-session-a")

    token = set_request_key("session-a")
    try:
        invalidate_session()
        invalidate_session()
    finally:
        reset_request_key(token)

    assert session_store.get("session-a") is None
    assert session_store.get_mcp_refresh_token(_hash("refresh-session-a")) is None


def test_invalidation_clears_session_when_grant_revocation_fails(monkeypatch):
    clear_calls = []

    def fail_revocation(_key):
        raise RuntimeError("mocked grant revocation failure")

    monkeypatch.setattr(session_store, "revoke_mcp_grants", fail_revocation)
    monkeypatch.setattr(session_store, "clear", clear_calls.append)

    token = set_request_key("session-a")
    try:
        with pytest.raises(RuntimeError, match="mocked grant revocation failure"):
            invalidate_session()
    finally:
        reset_request_key(token)

    assert clear_calls == ["session-a"]


@pytest.mark.asyncio
async def test_logout_revokes_grants_even_when_backend_logout_fails(monkeypatch):
    _create_grants("session-a", "user-session-a", "tenant-a", "membership-session-a")

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise RuntimeError("mocked backend failure")

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: FailingClient())
    monkeypatch.setattr(
        auth,
        "get_session",
        lambda: SimpleNamespace(get_refresh_token=lambda: "backend-refresh"),
    )
    token = set_request_key("session-a")
    try:
        with pytest.raises(RuntimeError, match="mocked backend failure"):
            await auth.logout()
    finally:
        reset_request_key(token)

    assert session_store.get_mcp_refresh_token(_hash("refresh-session-a")) is None
    assert session_store.get_mcp_token(_hash("access-session-a")) is None

