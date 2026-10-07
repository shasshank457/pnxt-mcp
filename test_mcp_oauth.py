"""Security tests for the MCP OAuth broker.

PointNXT network behavior is intentionally mocked here; see the integration
notes in the test output for the required live contract.
"""

import asyncio
import base64
import hashlib
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from services import mcp_oauth, session_store
from services.auth_session import set_session
from services.oauth_client_metadata import (
    ClientMetadataError,
    clear_metadata_cache_for_tests,
    resolve_client_metadata,
    validate_metadata,
    validate_metadata_url,
)
from services.request_context import reset_request_key, set_request_key


class Request:
    def __init__(self, query="", body=""):
        self.query_params = dict(item.split("=", 1) for item in query.split("&") if item)
        self._body = body.encode()

    async def body(self):
        return self._body


def verifier_pair():
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def response_location(response):
    return response.headers["location"]


class FakeResponse:
    def __init__(self, status_code=200, body=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    async def aiter_bytes(self):
        for index in range(0, len(self._body), 1024):
            yield self._body[index:index + 1024]


class FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class StreamingOversizedResponse(FakeResponse):
    def __init__(self):
        super().__init__(headers={})
        self.yielded_bytes = 0

    async def aiter_bytes(self):
        chunk = b"x" * 1024
        for _ in range(65):
            self.yielded_bytes += len(chunk)
            yield chunk


class FakeClient:
    responses = []
    requests = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        self.requests.append((url, kwargs))
        return FakeStream(self.responses.pop(0))


async def cimd_tests() -> None:
    clear_metadata_cache_for_tests()
    client_id = "https://client.example/.well-known/mcp-client.json"
    redirect = "https://client.example/oauth/callback"
    document = {
        "client_id": client_id,
        "client_name": "Example MCP client",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    for bad in (
        "http://client.example/client.json",
        "https://client.example",
        "https://client.example/",
        "https://user:pass@client.example/client.json",
        "https://client.example/client.json#fragment",
        "https://client.example/a/../client.json",
        "not-a-url",
    ):
        try:
            validate_metadata_url(bad)
        except ClientMetadataError:
            pass
        else:
            raise AssertionError(f"accepted unsafe metadata URL: {bad}")

    for bad_document in (
        {**document, "client_id": "https://other.example/client.json"},
        {key: value for key, value in document.items() if key != "client_id"},
        {key: value for key, value in document.items() if key != "client_name"},
        {key: value for key, value in document.items() if key != "redirect_uris"},
        {**document, "redirect_uris": ["http://client.example/callback"]},
        {**document, "grant_types": ["client_credentials"]},
        {**document, "response_types": ["token"]},
        {**document, "token_endpoint_auth_method": "client_secret_basic"},
    ):
        try:
            validate_metadata(client_id, bad_document)
        except ClientMetadataError:
            pass
        else:
            raise AssertionError("accepted invalid CIMD metadata")

    FakeClient.responses = [FakeResponse(body=__import__("json").dumps(document).encode())]
    FakeClient.requests = []
    with patch("services.oauth_client_metadata.httpx.AsyncClient", FakeClient), \
         patch("services.oauth_client_metadata.MCP_CIMD_ALLOWED_HOSTS", ("client.example",)), \
         patch("services.oauth_client_metadata._public_addresses", return_value=["203.0.113.10"]):
        resolved = await resolve_client_metadata(client_id)
        assert resolved["redirect_uris"] == document["redirect_uris"]
        assert len(FakeClient.requests) == 1
        assert await resolve_client_metadata(client_id) == resolved
        assert len(FakeClient.requests) == 1

    clear_metadata_cache_for_tests()
    FakeClient.responses = [FakeResponse(status_code=302, headers={"location": "https://private.example/client.json"})]
    with patch("services.oauth_client_metadata.httpx.AsyncClient", FakeClient), \
         patch("services.oauth_client_metadata.MCP_CIMD_ALLOWED_HOSTS", ("client.example",)), \
         patch("services.oauth_client_metadata._public_addresses", side_effect=[
             ["203.0.113.10"],
             ClientMetadataError("private address"),
         ]):
        try:
            await resolve_client_metadata(client_id)
        except ClientMetadataError:
            pass
        else:
            raise AssertionError("accepted redirect to restricted metadata host")

    clear_metadata_cache_for_tests()
    oversized = StreamingOversizedResponse()
    FakeClient.responses = [oversized]
    with patch("services.oauth_client_metadata.httpx.AsyncClient", FakeClient), \
         patch("services.oauth_client_metadata.MCP_CIMD_ALLOWED_HOSTS", ("client.example",)), \
         patch("services.oauth_client_metadata._public_addresses", return_value=["203.0.113.10"]):
        try:
            await resolve_client_metadata(client_id)
        except ClientMetadataError:
            pass
        else:
            raise AssertionError("accepted oversized metadata")
    assert oversized.yielded_bytes <= 64 * 1024 + 1024

    old_client, old_redirect = mcp_oauth.MCP_OAUTH_CLIENT_ID, mcp_oauth.MCP_OAUTH_REDIRECT_URI
    mcp_oauth.MCP_OAUTH_CLIENT_ID = "fixed-client"
    mcp_oauth.MCP_OAUTH_REDIRECT_URI = "https://fixed.example/callback"
    verifier, challenge = verifier_pair()
    query = "&".join((
        f"client_id={client_id}", f"redirect_uri={redirect}", "state=cimd-state",
        f"code_challenge={challenge}", "code_challenge_method=S256", "scope=mcp", f"resource={mcp_oauth.MCP_RESOURCE}",
    ))
    async def fake_metadata(value):
        if value != client_id:
            raise ClientMetadataError("unknown client")
        return document

    with patch("services.mcp_oauth.resolve_client_metadata", new=fake_metadata):
        assert (await mcp_oauth.authorize(Request(query))).status_code == 302
        assert session_store.has_oauth_transaction("cimd-state")
        assert (await mcp_oauth.authorize(Request(query.replace(f"&resource={mcp_oauth.MCP_RESOURCE}", "")))).status_code == 400
        assert (await mcp_oauth.authorize(Request(query.replace(mcp_oauth.MCP_RESOURCE, "https://other.example/mcp")))).status_code == 400
        assert (await mcp_oauth.authorize(Request(query.replace(redirect, "https://client.example/evil")))).status_code == 400
        assert (await mcp_oauth.authorize(Request(query.replace("client_id=" + client_id, "client_id=plain-client")))).status_code == 400
        transaction = session_store.consume_oauth_transaction("cimd-state")
        session_store.put_oauth_transaction("cimd-state", transaction, time.time() + 60)
        key_token = set_request_key(transaction["auth_session_key"])
        set_session({"data": {"accessToken": "pointnxt-access", "refreshToken": "pointnxt-refresh", "tenantId": "tenant-cimd", "userId": "user-cimd", "membershipId": "membership-cimd", "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}})
        reset_request_key(key_token)

        valid_verifier, valid_challenge = verifier_pair()
        record = {**transaction, "code_challenge": valid_challenge, "user_id": "user-cimd", "tenant_id": "tenant-cimd", "membership_id": "membership-cimd"}
        session_store.put_oauth_code(mcp_oauth._hash("cimd-valid"), record, time.time() + 60)
        valid_request = Request(body=f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect}&resource={mcp_oauth.MCP_RESOURCE}&code_verifier={valid_verifier}&code=cimd-valid")
        valid_response = await mcp_oauth.token(valid_request)
        assert valid_response.status_code == 200
        assert b"pointnxt-access" not in valid_response.body and b"pointnxt-refresh" not in valid_response.body
        access_token = __import__("json").loads(valid_response.body)["access_token"]
        assert mcp_oauth.validate_bearer(access_token, "cimd-transport")
        assert not mcp_oauth.validate_bearer(access_token, "other-cimd-transport")
        assert (await mcp_oauth.token(valid_request)).status_code == 400
        session_store.put_oauth_code(mcp_oauth._hash("missing-resource"), record, time.time() + 60)
        assert (await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect}&code_verifier={valid_verifier}&code=missing-resource"))).status_code == 400
        session_store.put_oauth_code(mcp_oauth._hash("wrong-resource"), record, time.time() + 60)
        assert (await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect}&resource=https://other.example/mcp&code_verifier={valid_verifier}&code=wrong-resource"))).status_code == 400

        _, bad_challenge = verifier_pair()
        session_store.put_oauth_code(mcp_oauth._hash("cimd-bad-pkce"), {**record, "code_challenge": bad_challenge}, time.time() + 60)
        bad_pkce = await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect}&resource={mcp_oauth.MCP_RESOURCE}&code_verifier=wrong&code=cimd-bad-pkce"))
        assert bad_pkce.status_code == 400
        session_store.put_oauth_code(mcp_oauth._hash("cimd-expired"), {**record, "code_challenge": valid_challenge}, time.time() - 1)
        assert (await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id={client_id}&redirect_uri={redirect}&resource={mcp_oauth.MCP_RESOURCE}&code_verifier={valid_verifier}&code=cimd-expired"))).status_code == 400
        session_store.put_oauth_code(mcp_oauth._hash("cimd-wrong-redirect"), record, time.time() + 60)
        assert (await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id={client_id}&redirect_uri=https://client.example/other&resource={mcp_oauth.MCP_RESOURCE}&code_verifier={valid_verifier}&code=cimd-wrong-redirect"))).status_code == 400
        session_store.put_oauth_code(mcp_oauth._hash("cimd-other-client"), record, time.time() + 60)
        assert (await mcp_oauth.token(Request(body=f"grant_type=authorization_code&client_id=https://other.example/client.json&redirect_uri={redirect}&resource={mcp_oauth.MCP_RESOURCE}&code_verifier={valid_verifier}&code=cimd-other-client"))).status_code == 401
    assert (await mcp_oauth.authorize(Request("client_id=fixed-client&redirect_uri=https://fixed.example/callback&state=fixed&code_challenge=" + challenge + "&code_challenge_method=S256&scope=mcp&resource=" + mcp_oauth.MCP_RESOURCE))).status_code == 302
    assert (await mcp_oauth.authorize(Request("client_id=fixed-client&redirect_uri=https://evil.example/callback&state=fixed-bad&code_challenge=" + challenge + "&code_challenge_method=S256&scope=mcp&resource=" + mcp_oauth.MCP_RESOURCE))).status_code == 400
    assert transaction["client_id"] == client_id and transaction["redirect_uri"] == redirect
    session_store.clear_all_for_tests()
    mcp_oauth.MCP_OAUTH_CLIENT_ID, mcp_oauth.MCP_OAUTH_REDIRECT_URI = old_client, old_redirect
    clear_metadata_cache_for_tests()


async def main() -> None:
    session_store.clear_all_for_tests()
    old_client, old_redirect = mcp_oauth.MCP_OAUTH_CLIENT_ID, mcp_oauth.MCP_OAUTH_REDIRECT_URI
    mcp_oauth.MCP_OAUTH_CLIENT_ID = "claude"
    mcp_oauth.MCP_OAUTH_REDIRECT_URI = "https://claude.example/callback"
    verifier, challenge = verifier_pair()

    def auth_query(**extra):
        values = {
            "client_id": "claude",
            "redirect_uri": "https://claude.example/callback",
            "state": "state-1",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp",
            "resource": mcp_oauth.MCP_RESOURCE,
        }
        values.update(extra)
        return "&".join(f"{key}={value}" for key, value in values.items())

    # Authorization does not need an MCP transport context.
    response = await mcp_oauth.authorize(Request(auth_query()))
    assert response.status_code == 302
    assert session_store.has_oauth_transaction("state-1")

    for query in (auth_query(client_id=""), auth_query(client_id="other"), auth_query(redirect_uri=""), auth_query(redirect_uri="https://evil"), auth_query(state=""), auth_query(code_challenge=""), auth_query(code_challenge_method="plain"), auth_query(scope="openid")):
        assert (await mcp_oauth.authorize(Request(query))).status_code == 400

    # Callback rejects direct PointNXT tokens on the OAuth path.
    with patch("tools.auth.auth_callback", new=AsyncMock()):
        assert (await mcp_oauth.callback(Request("state=state-1&accessToken=pointnxt"))).status_code == 400

    # Recreate a valid transaction after the direct-token rejection consumed none.
    response = await mcp_oauth.authorize(Request(auth_query(state="state-2")))
    assert response.status_code == 302
    transaction = session_store.consume_oauth_transaction("state-2")
    assert transaction
    session_key = transaction["auth_session_key"]
    token = set_request_key(session_key)
    set_session({"data": {"accessToken": "pointnxt-access", "refreshToken": "pointnxt-refresh", "tenantId": "tenant-a", "userId": "user-a", "membershipId": "membership-a", "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}})
    reset_request_key(token)
    session_store.put_oauth_transaction("state-2", transaction, time.time() + 60)

    with patch("tools.auth.auth_callback", new=AsyncMock(return_value=SimpleNamespace(status_code=200))):
        callback_response = await mcp_oauth.callback(Request("state=state-2&code=pointnxt-code"))
    assert callback_response.status_code == 302
    location = response_location(callback_response)
    assert "iss=" in location and mcp_oauth.MCP_PUBLIC_BASE_URL in unquote(location)
    code = location.split("code=", 1)[1].split("&", 1)[0]
    assert len(code) > 40
    stored_code = session_store.consume_oauth_code(mcp_oauth._hash(code))
    assert stored_code and stored_code["tenant_id"] == "tenant-a"
    # Put a code back for exchange tests; the stored value is intentionally hashed.
    session_store.put_oauth_code(mcp_oauth._hash(code), {**transaction, "auth_session_key": session_key, "user_id": "user-a", "tenant_id": "tenant-a", "membership_id": "membership-a"}, time.time() + 60)

    token_response = await mcp_oauth.token(Request(body="grant_type=authorization_code&client_id=claude&redirect_uri=https%3A%2F%2Fclaude.example%2Fcallback&resource=" + mcp_oauth.MCP_RESOURCE + "&code_verifier=" + verifier + "&code=" + code))
    assert token_response.status_code == 200
    access_token = token_response.body.decode().split('"access_token":"', 1)[1].split('"', 1)[0]
    assert "pointnxt-access" not in access_token and "pointnxt-refresh" not in access_token

    assert mcp_oauth.validate_bearer(access_token, "transport-a")
    assert not mcp_oauth.validate_bearer(access_token, "transport-b")
    assert not mcp_oauth.validate_bearer(access_token, "transport-a") is False

    # Identity mismatch and tenant/membership mismatch are rejected.
    for field, value in (("user_id", "user-b"), ("tenant_id", "tenant-b"), ("membership_id", "membership-b")):
        bad = {"auth_session_key": session_key, "scope": "mcp", "user_id": "user-a", "tenant_id": "tenant-a", "membership_id": "membership-a"}
        bad[field] = value
        session_store.put_mcp_token(mcp_oauth._hash(field), bad, time.time() + 60)
        assert not mcp_oauth.validate_bearer(field, "transport-")

    # Expiry and revocation.
    session_store.put_mcp_token(mcp_oauth._hash("expired"), {"auth_session_key": session_key, "user_id": "user-a", "tenant_id": "tenant-a", "membership_id": "membership-a"}, time.time() - 1)
    assert not mcp_oauth.validate_bearer("expired", "transport-expired")
    session_store.put_mcp_token(mcp_oauth._hash("revoked"), {"auth_session_key": session_key, "user_id": "user-a", "tenant_id": "tenant-a", "membership_id": "membership-a"}, time.time() + 60)
    session_store.revoke_mcp_token(mcp_oauth._hash("revoked"))
    assert not mcp_oauth.validate_bearer("revoked", "transport-revoked")

    # SQLite transaction consumption allows exactly one concurrent redemption.
    session_store.put_oauth_code("race", {"client_id": "claude"}, time.time() + 60)
    results = await asyncio.gather(asyncio.to_thread(session_store.consume_oauth_code, "race"), asyncio.to_thread(session_store.consume_oauth_code, "race"))
    assert sum(result is not None for result in results) == 1

    protected = await mcp_oauth.metadata_protected_resource(None)
    authorization = await mcp_oauth.metadata_authorization_server(None)
    assert protected.status_code == authorization.status_code == 200
    assert b"refresh_token" not in authorization.body
    assert b"S256" in authorization.body
    assert b'"client_id_metadata_document_supported":true' in authorization.body
    assert b"registration_endpoint" not in authorization.body
    assert b'"authorization_response_iss_parameter_supported":true' in authorization.body

    await cimd_tests()

    mcp_oauth.MCP_OAUTH_CLIENT_ID, mcp_oauth.MCP_OAUTH_REDIRECT_URI = old_client, old_redirect
    session_store.clear_all_for_tests()
    print("MCP OAuth security tests passed")


if __name__ == "__main__":
    asyncio.run(main())
