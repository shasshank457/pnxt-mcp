import base64
import hashlib
import json
import time

import pytest

from services import mcp_oauth, oauth_client_metadata, session_store
from test_mcp_oauth import Request

CLIENT_ID = "https://chatgpt.com/oauth/demo/client.json"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).rstrip(b"=").decode()


def _metadata():
    return {
        "client_id": CLIENT_ID,
        "client_name": "ChatGPT",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def _query(**values):
    return "&".join(f"{key}={value}" for key, value in values.items())


@pytest.mark.asyncio
async def test_chatgpt_cimd_authorize_and_token(monkeypatch):
    session_store.clear_all_for_tests()

    async def resolve(client_id):
        assert client_id == CLIENT_ID
        return _metadata()

    monkeypatch.setattr(mcp_oauth, "resolve_client_metadata", resolve)
    query = _query(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state="chatgpt-state",
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        scope="mcp",
        resource=mcp_oauth.MCP_RESOURCE,
    )
    response = await mcp_oauth.authorize(Request(query))
    assert response.status_code == 302
    transaction = session_store.consume_oauth_transaction("chatgpt-state")
    assert transaction["client_id"] == CLIENT_ID

    session_store.put_oauth_code(
        mcp_oauth._hash("chatgpt-code"),
        {
            **transaction,
            "auth_session_key": transaction["auth_session_key"],
            "user_id": "user-chatgpt",
            "tenant_id": "tenant-chatgpt",
            "membership_id": "membership-chatgpt",
        },
        time.time() + 60,
    )
    body = _query(
        grant_type="authorization_code",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        resource=mcp_oauth.MCP_RESOURCE,
        code_verifier=VERIFIER,
        code="chatgpt-code",
    )
    token = await mcp_oauth.token(Request(body=body))
    assert token.status_code == 200
    assert json.loads(token.body)["token_type"] == "Bearer"


@pytest.mark.asyncio
async def test_chatgpt_cimd_rejects_unregistered_redirect(monkeypatch):
    async def resolve(_client_id):
        return _metadata()

    monkeypatch.setattr(mcp_oauth, "resolve_client_metadata", resolve)
    response = await mcp_oauth.authorize(
        Request(
            _query(
                client_id=CLIENT_ID,
                redirect_uri="https://chatgpt.com/other",
                state="bad-redirect",
                code_challenge=CHALLENGE,
                code_challenge_method="S256",
                scope="mcp",
                resource=mcp_oauth.MCP_RESOURCE,
            )
        )
    )
    payload = json.loads(response.body)
    assert response.status_code == 400
    assert payload["error"] == "invalid_request"
    assert "redirect" in payload["error_description"]


@pytest.mark.asyncio
async def test_cimd_rejects_missing_pkce(monkeypatch):
    async def resolve(_client_id):
        return _metadata()

    monkeypatch.setattr(mcp_oauth, "resolve_client_metadata", resolve)
    response = await mcp_oauth.authorize(
        Request(
            _query(
                client_id=CLIENT_ID,
                redirect_uri=REDIRECT_URI,
                state="missing-pkce",
                scope="mcp",
                resource=mcp_oauth.MCP_RESOURCE,
            )
        )
    )
    payload = json.loads(response.body)
    assert response.status_code == 400
    assert payload["error_description"] == "missing code_challenge"


def test_cimd_rejects_non_allowlisted_host(monkeypatch):
    monkeypatch.setattr(
        oauth_client_metadata, "MCP_CIMD_ALLOWED_HOSTS", ("chatgpt.com",)
    )
    with pytest.raises(oauth_client_metadata.ClientMetadataError, match="allowlisted"):
        oauth_client_metadata._public_addresses("evil.example")

