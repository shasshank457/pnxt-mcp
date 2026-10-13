"""Pytest-collected tests for the MCP OAuth consent transaction."""

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from services import mcp_oauth, session_store


class Request:
    def __init__(self, query="", body="", scheme="https"):
        self.query_params = {}
        if query:
            self.query_params = dict(item.split("=", 1) for item in query.split("&"))
        self._body = body.encode()
        self.url = SimpleNamespace(scheme=scheme)

    async def body(self):
        return self._body


def _payload(**overrides):
    result = {
        "state": "consent-state",
        "client_id": "claude",
        "redirect_uri": "https://client.example/callback",
        "resource": mcp_oauth.MCP_RESOURCE,
        "scope": "mcp",
        "auth_session_key": "consent-session",
        "user_id": "user-a",
        "tenant_id": "tenant-a",
        "membership_id": "membership-a",
        "client_display_name": "Claude & PointNXT",
    }
    result.update(overrides)
    return result


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    session_store.clear_all_for_tests()
    session = SimpleNamespace(
        user_id="user-a",
        tenant_id="tenant-a",
        membership_id="membership-a",
        is_authenticated=lambda: True,
    )
    monkeypatch.setattr(session_store, "get", lambda _key: session)
    yield
    session_store.clear_all_for_tests()


def _put(consent_id="consent-1", expires_at=None, **overrides):
    session_store.put_oauth_consent_transaction(
        consent_id,
        _payload(**overrides),
        expires_at if expires_at is not None else time.time() + 60,
    )


def _approve_request(consent_id="consent-1", decision="approved"):
    return Request(
        body=f"consent_id={consent_id}&decision={decision}",
    )


@pytest.mark.asyncio
async def test_consent_page_displays_safe_application_and_capabilities():
    _put(redirect_uri="https://claude.ai/api/mcp/auth_callback")
    response = await mcp_oauth.consent(Request(query="consent_id=consent-1"))
    body = response.body.decode()
    assert response.status_code == 200
    assert "form-action 'self' https://claude.ai" in response.headers[
        "content-security-policy"
    ]
    assert "Claude &amp; PointNXT" in body
    assert "PointNXT MCP" in body
    assert "Scope: <code>mcp</code>" in body
    assert "orders" in body and "inventory" in body and "shipments" in body
    assert "refund" in body and "cancellation" in body
    assert 'value="approved"' in body
    assert 'value="denied"' in body


@pytest.mark.asyncio
async def test_approval_issues_one_authorization_code():
    _put()
    response = await mcp_oauth.consent_decision(_approve_request())
    assert response.status_code == 302
    location = response.headers["location"]
    assert "code=" in location and "state=consent-state" in location
    assert session_store.get_oauth_consent_transaction("consent-1") is None
    assert session_store.consume_oauth_code(mcp_oauth._hash(location.split("code=", 1)[1].split("&", 1)[0]))


@pytest.mark.asyncio
async def test_denial_redirects_access_denied_without_code():
    _put()
    response = await mcp_oauth.consent_decision(_approve_request(decision="denied"))
    assert response.status_code == 302
    location = response.headers["location"]
    assert "error=access_denied" in location
    assert "code=" not in location
    assert session_store.get_oauth_consent_transaction("consent-1") is None


@pytest.mark.asyncio
async def test_expired_and_replayed_consent_cannot_issue_code():
    _put(expires_at=time.time() - 1)
    assert (await mcp_oauth.consent(Request(query="consent_id=consent-1"))).status_code == 400
    assert (await mcp_oauth.consent_decision(_approve_request())).status_code == 400

    _put()
    first = await mcp_oauth.consent_decision(_approve_request())
    replay = await mcp_oauth.consent_decision(_approve_request())
    assert first.status_code == 302
    assert replay.status_code == 400


@pytest.mark.asyncio
async def test_concurrent_approval_and_binding_mismatches_fail_closed():
    _put("race")
    results = await asyncio.gather(
        mcp_oauth.consent_decision(_approve_request("race")),
        mcp_oauth.consent_decision(_approve_request("race")),
    )
    assert sum(result.status_code == 302 for result in results) == 1

    for field, value in (
        ("state", "other-state"),
        ("client_id", "other-client"),
        ("redirect_uri", "https://evil.example/callback"),
        ("resource", "https://other.example/mcp"),
        ("auth_session_key", "other-session"),
        ("user_id", "user-b"),
        ("tenant_id", "tenant-b"),
        ("membership_id", "membership-b"),
    ):
        _put(field)
        payload = _payload()
        expected = {
            "client_id": payload["client_id"],
            "state": payload["state"],
            "redirect_uri": payload["redirect_uri"],
            "resource": payload["resource"],
            "scope": payload["scope"],
            "auth_session_key": payload["auth_session_key"],
            "user_id": payload["user_id"],
            "tenant_id": payload["tenant_id"],
            "membership_id": payload["membership_id"],
        }
        expected[field] = value
        assert session_store.consume_oauth_consent_transaction(
            field, **expected, decision="approved"
        ) is None
        assert session_store.get_oauth_consent_transaction(field) is not None


@pytest.mark.asyncio
async def test_consent_requires_https():
    _put()
    response = await mcp_oauth.consent(Request(query="consent_id=consent-1", scheme="http"))
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_full_oauth_callback_consent_and_pkce_exchange(monkeypatch):
    verifier = "v" * 64
    import base64
    import hashlib

    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    monkeypatch.setattr(mcp_oauth, "MCP_OAUTH_CLIENT_ID", "claude")
    monkeypatch.setattr(
        mcp_oauth, "MCP_OAUTH_REDIRECT_URI", "https://client.example/callback"
    )
    session = SimpleNamespace(
        user_id="user-a",
        tenant_id="tenant-a",
        membership_id="membership-a",
        is_authenticated=lambda: True,
    )
    monkeypatch.setattr(mcp_oauth, "get_session", lambda: session)
    monkeypatch.setattr(session_store, "get", lambda _key: session)

    auth_request = Request(
        query=(
            "client_id=claude&redirect_uri=https://client.example/callback&"
            "state=integration-state&code_challenge="
            f"{challenge}&code_challenge_method=S256&scope=mcp&"
            f"resource={mcp_oauth.MCP_RESOURCE}"
        )
    )
    authorization = await mcp_oauth.authorize(auth_request)
    assert authorization.status_code == 302
    assert "/auth/login" in authorization.headers["location"]

    from unittest.mock import AsyncMock, patch

    with patch(
        "tools.auth.auth_callback",
        new=AsyncMock(return_value=SimpleNamespace(status_code=200)),
    ):
        callback = await mcp_oauth.callback(
            Request(query="state=integration-state&code=backend-code")
        )
    assert callback.status_code == 302
    consent_location = callback.headers["location"]
    assert "/oauth/consent" in consent_location
    assert "code=" not in consent_location
    consent_id = parse_qs(urlsplit(consent_location).query)["consent_id"][0]

    consent_page = await mcp_oauth.consent(
        Request(query=f"consent_id={consent_id}")
    )
    assert consent_page.status_code == 200
    assert "Claude" in consent_page.body.decode()

    approval = await mcp_oauth.consent_decision(
        Request(body=f"consent_id={consent_id}&decision=approved")
    )
    assert approval.status_code == 302
    code = parse_qs(urlsplit(approval.headers["location"]).query)["code"][0]
    assert session_store.consume_oauth_code(mcp_oauth._hash(code))

    session_store.put_oauth_code(
        mcp_oauth._hash(code),
        {
            "state": "integration-state",
            "client_id": "claude",
            "redirect_uri": "https://client.example/callback",
            "resource": mcp_oauth.MCP_RESOURCE,
            "scope": "mcp",
            "auth_session_key": "oauth-integration",
            "code_challenge": challenge,
            "user_id": "user-a",
            "tenant_id": "tenant-a",
            "membership_id": "membership-a",
        },
        time.time() + 60,
    )
    token_response = await mcp_oauth.token(
        Request(
            body=(
                "grant_type=authorization_code&client_id=claude&"
                "redirect_uri=https://client.example/callback&"
                f"resource={mcp_oauth.MCP_RESOURCE}&code_verifier={verifier}&code={code}"
            )
        )
    )
    assert token_response.status_code == 200
    token_body = json.loads(token_response.body)
    assert token_body["access_token"]
    assert token_body["refresh_token"]
    assert "backend-access" not in token_response.body.decode()
    assert "backend-refresh" not in token_response.body.decode()
    assert (await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id=claude&redirect_uri=https://client.example/callback&resource={mcp_oauth.MCP_RESOURCE}&code_verifier={verifier}&code={code}"))).status_code == 400
