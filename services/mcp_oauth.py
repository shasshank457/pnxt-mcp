"""OAuth 2.0 Authorization Code + PKCE adapter for the MCP server."""
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.responses import JSONResponse, RedirectResponse

from config import (
    AUTH_CALLBACK_TIMEOUT,
    MCP_OAUTH_CLIENT_ID,
    MCP_OAUTH_CODE_TTL,
    MCP_OAUTH_REDIRECT_URI,
    MCP_OAUTH_TOKEN_TTL,
    MCP_PUBLIC_BASE_URL,
)
from services import auth_bridge, session_store
from services.auth_session import get_session
from services.oauth_client_metadata import (
    ClientMetadataError,
    resolve_client_metadata,
)
from services.request_context import reset_request_key, set_request_key

MCP_SCOPE = "mcp"
MCP_RESOURCE = f"{MCP_PUBLIC_BASE_URL}/mcp"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _client_ok(client_id: str, redirect_uri: str) -> bool:
    try:
        is_https = urlsplit(redirect_uri).scheme == "https"
    except ValueError:
        is_https = False
    return (
        secrets.compare_digest(client_id, MCP_OAUTH_CLIENT_ID)
        and bool(MCP_OAUTH_REDIRECT_URI)
        and is_https
        and secrets.compare_digest(redirect_uri, MCP_OAUTH_REDIRECT_URI)
    )


async def _resolve_client(client_id: str, redirect_uri: str) -> bool:
    if _client_ok(client_id, redirect_uri):
        return True
    try:
        metadata = await resolve_client_metadata(client_id)
    except ClientMetadataError:
        return False
    return redirect_uri in metadata["redirect_uris"]


async def metadata_protected_resource(_request):
    return JSONResponse({
        "resource": MCP_RESOURCE,
        "authorization_servers": [MCP_PUBLIC_BASE_URL],
        "bearer_methods_supported": ["header"],
    })


async def metadata_authorization_server(_request):
    return JSONResponse({
        "issuer": MCP_PUBLIC_BASE_URL,
        "authorization_endpoint": f"{MCP_PUBLIC_BASE_URL}/oauth/authorize",
        "token_endpoint": f"{MCP_PUBLIC_BASE_URL}/oauth/token",
        "revocation_endpoint": f"{MCP_PUBLIC_BASE_URL}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [MCP_SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    })


async def authorize(request):
    query = request.query_params
    client_id = query.get("client_id", "")
    redirect_uri = query.get("redirect_uri", "")
    state = query.get("state", "")
    challenge = query.get("code_challenge", "")
    method = query.get("code_challenge_method", "")
    scope = query.get("scope", MCP_SCOPE)
    resource = query.get("resource", "")
    if scope != MCP_SCOPE:
        return JSONResponse({"error": "invalid_scope"}, status_code=400)
    if resource != MCP_RESOURCE:
        return JSONResponse({"error": "invalid_target"}, status_code=400)
    if not state or not challenge or method != "S256" or not await _resolve_client(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    transaction_id = secrets.token_urlsafe(32)
    auth_session_key = f"oauth:{transaction_id}"
    transaction = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "transaction_id": transaction_id,
        "auth_session_key": auth_session_key,
        "code_challenge": challenge,
        "scope": scope,
        "resource": resource,
    }
    session_store.put_oauth_transaction(state, transaction, time.time() + AUTH_CALLBACK_TIMEOUT)
    # The existing PointNXT callback transaction uses the durable OAuth
    # authorization-session key and performs the existing server-side exchange.
    session_store.put_auth_transaction(state, auth_session_key, {}, time.monotonic() + AUTH_CALLBACK_TIMEOUT)
    auth_bridge.begin(state, auth_session_key, {}, time.time() + AUTH_CALLBACK_TIMEOUT)
    return RedirectResponse(auth_bridge.login_url(state), status_code=302)


async def callback(request):
    from tools.auth import auth_callback

    state = request.query_params.get("state", "")
    if request.query_params.get("accessToken") or request.query_params.get("access_token"):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    transaction = session_store.consume_oauth_transaction(state)
    if not transaction:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    key = transaction["auth_session_key"]
    if request.query_params.get("bridge") == "1":
        completion = session_store.consume_auth_bridge_completion(state)
        if not completion or completion["request_key"] != key:
            return JSONResponse({"error": "access_denied"}, status_code=403)
    else:
        result = await auth_callback(request)
        if getattr(result, "status_code", 500) >= 400:
            return result
    token = set_request_key(key)
    try:
        session = get_session()
        if not session or not session.is_authenticated():
            return JSONResponse({"error": "access_denied"}, status_code=403)
        code = secrets.token_urlsafe(48)
        session_store.put_oauth_code(_hash(code), {
            **transaction,
            "auth_session_key": key,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "membership_id": session.membership_id,
        }, time.time() + MCP_OAUTH_CODE_TTL)
    finally:
        reset_request_key(token)
    return RedirectResponse(
        f"{transaction['redirect_uri']}?{urlencode({'code': code, 'state': state, 'iss': MCP_PUBLIC_BASE_URL})}",
        status_code=302,
    )


async def token(request):
    values = {key: item[0] for key, item in parse_qs((await request.body()).decode()).items()}
    if values.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    client_id = values.get("client_id", "")
    redirect_uri = values.get("redirect_uri", "")
    resource = values.get("resource", "")
    if resource != MCP_RESOURCE:
        return JSONResponse({"error": "invalid_target"}, status_code=400)
    fixed_client = _client_ok(client_id, redirect_uri)
    if not fixed_client:
        try:
            metadata = await resolve_client_metadata(client_id)
        except ClientMetadataError:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if metadata.get("client_id") != client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
    record = session_store.consume_oauth_code(_hash(values.get("code", "")))
    if (
        not record
        or record["client_id"] != client_id
        or record["redirect_uri"] != redirect_uri
        or record.get("resource") != resource
    ):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    verifier = values.get("code_verifier", "")
    expected = __import__("base64").urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not verifier or not secrets.compare_digest(expected, record["code_challenge"]):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    access_token = secrets.token_urlsafe(48)
    expires_at = time.time() + MCP_OAUTH_TOKEN_TTL
    session_store.put_mcp_token(_hash(access_token), {"auth_session_key": record["auth_session_key"], "scope": record["scope"], "resource": resource, "user_id": record["user_id"], "tenant_id": record["tenant_id"], "membership_id": record["membership_id"]}, expires_at)
    return JSONResponse({"access_token": access_token, "token_type": "Bearer", "expires_in": MCP_OAUTH_TOKEN_TTL, "scope": record["scope"]})


async def revoke(request):
    values = {key: item[0] for key, item in parse_qs((await request.body()).decode()).items()}
    if values.get("token"):
        session_store.revoke_mcp_token(_hash(values["token"]))
    return JSONResponse({}, status_code=200)


def validate_bearer(token: str, session_key: str) -> bool:
    record = session_store.bind_mcp_token(_hash(token), session_key)
    if not record:
        return False
    durable = session_store.get(record["auth_session_key"])
    if not durable:
        return False
    if not all(
        (
            record.get("resource") == MCP_RESOURCE,
            durable.user_id == record["user_id"],
            durable.tenant_id == record["tenant_id"],
            durable.membership_id == record["membership_id"],
            durable.is_authenticated(),
        )
    ):
        return False
    session_store.set_(session_key, durable)
    return True
