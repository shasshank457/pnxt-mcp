import base64
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from services import mcp_oauth, session_store
from services.mcp_session_middleware import MCPSessionMiddleware
from services.tool_errors import StructuredToolError
from test_mcp_oauth import Request


class _DurableSession:
    access_token = "pointnxt-access"
    refresh_token = "pointnxt-refresh"
    user_id = "user-a"
    tenant_id = "tenant-a"
    membership_id = "membership-a"
    expires_at = None
    created_at = None
    last_used_at = None

    def is_authenticated(self):
        return True


class _Context:
    def __init__(self, method, token, session_id="transport-a"):
        self.method = method
        self.protocol_version = "2025-06-18"
        self.request = SimpleNamespace(
            headers={
                "authorization": f"Bearer {token}",
                "mcp-protocol-version": "2025-06-18",
            }
        )
        self.session = SimpleNamespace(
            _connection=SimpleNamespace(session_id=session_id)
        )


async def _issue_access_token(monkeypatch):
    monkeypatch.setattr(mcp_oauth, "MCP_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setattr(
        mcp_oauth, "MCP_OAUTH_REDIRECT_URI", "https://client.example/callback"
    )
    # The verifier is deliberately paired with the stored challenge.
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    session_store.put_oauth_code(
        mcp_oauth._hash("test-code"),
        {
            "client_id": "test-client",
            "redirect_uri": "https://client.example/callback",
            "resource": mcp_oauth.MCP_RESOURCE,
            "scope": "mcp",
            "code_challenge": challenge,
            "auth_session_key": "oauth-session",
            "user_id": "user-a",
            "tenant_id": "tenant-a",
            "membership_id": "membership-a",
        },
        time.time() + 60,
    )
    response = await mcp_oauth.token(
        Request(
            body=(
                "grant_type=authorization_code&client_id=test-client&"
                "redirect_uri=https://client.example/callback&"
                f"resource={mcp_oauth.MCP_RESOURCE}&code_verifier={verifier}&"
                "code=test-code"
            )
        )
    )
    assert response.status_code == 200
    return json.loads(response.body)["access_token"]


@pytest.mark.asyncio
async def test_issued_token_authenticates_initialize_and_tools_list(monkeypatch):
    session_store.clear_all_for_tests()
    token = await _issue_access_token(monkeypatch)
    monkeypatch.setattr(session_store, "get", lambda _key: _DurableSession())
    monkeypatch.setattr(session_store, "set_", lambda _key, session: session)
    middleware = MCPSessionMiddleware()

    async def call_next(ctx):
        return {"method": ctx.method, "ok": True}

    for method, session_id in (
        ("initialize", "transport-a"),
        ("initialize", "transport-b"),
        ("tools/list", "transport-b"),
    ):
        result = await middleware(_Context(method, token, session_id), call_next)
        assert result == {"method": method, "ok": True}


@pytest.mark.asyncio
async def test_invalid_expired_and_wrong_audience_tokens_are_rejected(monkeypatch):
    session_store.clear_all_for_tests()
    monkeypatch.setattr(session_store, "get", lambda _key: _DurableSession())
    middleware = MCPSessionMiddleware()

    async def call_next(_ctx):
        return {"ok": True}

    for value, resource, expiry in (
        ("expired-token", mcp_oauth.MCP_RESOURCE, time.time() - 1),
        ("wrong-audience-token", "https://other.example/mcp", time.time() + 60),
    ):
        session_store.put_mcp_token(
            mcp_oauth._hash(value),
            {
                "auth_session_key": "oauth-session",
                "resource": resource,
                "user_id": "user-a",
                "tenant_id": "tenant-a",
                "membership_id": "membership-a",
            },
            expiry,
        )
        with pytest.raises(StructuredToolError, match="Invalid MCP access token"):
            await middleware(_Context("initialize", value), call_next)

