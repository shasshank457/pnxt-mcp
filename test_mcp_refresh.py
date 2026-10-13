"""Tests for Claude-facing MCP refresh-token rotation."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import mcp_oauth, session_store
from services.auth_session import set_session
from services.request_context import reset_request_key, set_request_key


class Request:
    def __init__(self, body):
        self._body = body.encode()

    async def body(self):
        return self._body


def _session(key="refresh-session", *, expires_at=None):
    token = set_request_key(key)
    try:
        set_session(
            {
                "data": {
                    "accessToken": "backend-access",
                    "refreshToken": "backend-refresh",
                    "tenantId": "tenant-a",
                    "userId": "user-a",
                    "membershipId": "membership-a",
                    "expiresAt": expires_at
                    or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                }
            }
        )
    finally:
        reset_request_key(token)


def _refresh_record(token="old-refresh", key="refresh-session", expires_at=None):
    session_store.put_mcp_refresh_token(
        mcp_oauth._hash(token),
        {
            "auth_session_key": key,
            "client_id": "claude",
            "scope": "mcp",
            "resource": mcp_oauth.MCP_RESOURCE,
            "user_id": "user-a",
            "tenant_id": "tenant-a",
            "membership_id": "membership-a",
        },
        expires_at if expires_at is not None else time.time() + 60,
    )


def _request(token="old-refresh", **extra):
    values = {
        "grant_type": "refresh_token",
        "client_id": "claude",
        "resource": mcp_oauth.MCP_RESOURCE,
        "refresh_token": token,
    }
    values.update(extra)
    return Request("&".join(f"{key}={value}" for key, value in values.items()))


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    session_store.clear_all_for_tests()
    live = SimpleNamespace(
        user_id="user-a", tenant_id="tenant-a", membership_id="membership-a",
        is_authenticated=lambda: True,
    )
    monkeypatch.setattr(mcp_oauth, "get_session", lambda _key: live)
    monkeypatch.setattr(session_store, "get", lambda _key: live)
    yield
    session_store.clear_all_for_tests()
    reset_request_key(set_request_key(None))


@pytest.mark.asyncio
async def test_refresh_issues_rotated_tokens_without_backend_tokens():
    _refresh_record()
    response = await mcp_oauth.token(_request())
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["access_token"]
    assert body["refresh_token"]
    assert "backend-access" not in response.body.decode()
    assert "backend-refresh" not in response.body.decode()
    assert (await mcp_oauth.token(_request())).status_code == 400
    assert (await mcp_oauth.token(_request(body["refresh_token"]))).status_code == 200


@pytest.mark.asyncio
async def test_refresh_rejects_expired_revoked_and_mismatched_grants(monkeypatch):
    _refresh_record("expired", expires_at=time.time() - 1)
    assert (await mcp_oauth.token(_request("expired"))).status_code == 400

    _refresh_record("revoked")
    session_store.revoke_mcp_refresh_token(mcp_oauth._hash("revoked"))
    assert (await mcp_oauth.token(_request("revoked"))).status_code == 400

    _refresh_record("wrong-client")
    assert (await mcp_oauth.token(_request("wrong-client", client_id="other"))).status_code == 400

    _refresh_record("wrong-resource")
    assert (await mcp_oauth.token(_request("wrong-resource", resource="https://other.example/mcp"))).status_code == 400

    _refresh_record("expired-session", key="expired-session")
    expired = SimpleNamespace(is_authenticated=lambda: False)
    monkeypatch.setattr(mcp_oauth, "get_session", lambda _key: expired)
    monkeypatch.setattr(session_store, "get", lambda _key: expired)
    assert (await mcp_oauth.token(_request("expired-session"))).status_code == 400


@pytest.mark.asyncio
async def test_refresh_rotation_is_atomic_under_concurrency():
    _refresh_record("race")
    results = await asyncio.gather(
        mcp_oauth.token(_request("race")),
        mcp_oauth.token(_request("race")),
    )
    assert sum(result.status_code == 200 for result in results) == 1


@pytest.mark.asyncio
async def test_metadata_advertises_refresh_only_when_supported():
    response = await mcp_oauth.metadata_authorization_server(None)
    assert b"refresh_token" in response.body
