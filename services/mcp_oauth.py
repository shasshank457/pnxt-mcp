"""OAuth 2.0 Authorization Code + PKCE adapter for the MCP server."""
import base64
import hashlib
import html
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import (
    AUTH_CALLBACK_TIMEOUT,
    CHATGPT_REDIRECT_URI,
    MCP_AUTO_APPROVE_CLIENT_IDS,
    MCP_OAUTH_CLIENT_ID,
    MCP_OAUTH_CODE_TTL,
    MCP_OAUTH_REDIRECT_URI,
    MCP_OAUTH_TOKEN_TTL,
    MCP_PUBLIC_BASE_URL,
    OAUTH_ALLOWED_REDIRECT_URIS,
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
MCP_REFRESH_TOKEN_TTL = MCP_OAUTH_TOKEN_TTL * 30
logger = logging.getLogger(__name__)
_CHATGPT_REDIRECT_PATTERN = re.compile(
    r"^https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]+$"
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status_code
    )


def _resource_matches(value: str) -> bool:
    return value.rstrip("/") == MCP_RESOURCE.rstrip("/")


def _known_redirect_uri(value: str) -> bool:
    return value in OAUTH_ALLOWED_REDIRECT_URIS or value == MCP_OAUTH_REDIRECT_URI or (
        CHATGPT_REDIRECT_URI and value == CHATGPT_REDIRECT_URI
    ) or bool(_CHATGPT_REDIRECT_PATTERN.fullmatch(value))


_CONSENT_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _with_form_action_origin(
    base_headers: dict[str, str], redirect_uri: str
) -> dict[str, str]:
    headers = base_headers.copy()
    parsed = urlsplit(redirect_uri)
    if parsed.scheme and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers["Content-Security-Policy"] = headers[
            "Content-Security-Policy"
        ].replace("form-action 'self'", f"form-action 'self' {origin}")
    return headers


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
        and _known_redirect_uri(redirect_uri)
    )


async def _resolve_client(client_id: str, redirect_uri: str) -> bool:
    if _client_ok(client_id, redirect_uri):
        return True
    try:
        metadata = await resolve_client_metadata(client_id)
    except ClientMetadataError:
        return False
    return redirect_uri in metadata["redirect_uris"]


async def _client_display_name(client_id: str, redirect_uri: str) -> str:
    if _client_ok(client_id, redirect_uri):
        return "Claude"
    try:
        metadata = await resolve_client_metadata(client_id)
    except ClientMetadataError:
        return "OAuth client"
    name = metadata.get("client_name")
    return name.strip() if isinstance(name, str) and name.strip() else "OAuth client"


def _https_request(request) -> bool:
    return getattr(getattr(request, "url", None), "scheme", "") == "https"


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
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [MCP_SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    })


def _issue_authorization_code(transaction: dict) -> RedirectResponse:
    code = secrets.token_urlsafe(48)
    session_store.put_oauth_code(
        _hash(code), transaction, time.time() + MCP_OAUTH_CODE_TTL
    )
    return RedirectResponse(
        f"{transaction['redirect_uri']}?{urlencode({'code': code, 'state': transaction['state'], 'iss': MCP_PUBLIC_BASE_URL})}",
        status_code=302,
    )


async def authorize(request):
    query = request.query_params
    client_id = query.get("client_id", "")
    redirect_uri = query.get("redirect_uri", "")
    state = query.get("state", "")
    challenge = query.get("code_challenge", "")
    method = query.get("code_challenge_method", "")
    scope = MCP_SCOPE
    resource = query.get("resource", "")
    # This server exposes one scope. Unknown requested scopes are ignored and
    # the server's default scope is used, without granting additional access.
    if not _resource_matches(resource):
        logger.info("OAuth authorize rejected: invalid resource client_id=%s redirect_uri=%s", client_id, redirect_uri)
        return _oauth_error("invalid_target", "resource must identify this MCP server")
    if not state:
        reason = "missing state"
    elif not challenge:
        reason = "missing code_challenge"
    elif method != "S256":
        reason = "code_challenge_method must be S256"
    else:
        try:
            client_valid = await _resolve_client(client_id, redirect_uri)
        except ClientMetadataError as exc:
            logger.info("OAuth authorize rejected: CIMD validation failed: %s client_id=%s redirect_uri=%s", exc, client_id, redirect_uri)
            return _oauth_error("invalid_request", str(exc))
        if not client_valid:
            reason = "client_id or redirect_uri is not authorized"
        else:
            reason = ""
    if reason:
        logger.info("OAuth authorize rejected: %s client_id=%s redirect_uri=%s", reason, client_id, redirect_uri)
        return _oauth_error("invalid_request", reason)
    transaction_id = secrets.token_urlsafe(32)
    auth_session_key = f"oauth:{transaction_id}"
    transaction = {
        "state": state,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "transaction_id": transaction_id,
        "auth_session_key": auth_session_key,
        "code_challenge": challenge,
        "scope": scope,
        "resource": MCP_RESOURCE,
    }
    session_store.put_oauth_transaction(state, transaction, time.time() + AUTH_CALLBACK_TIMEOUT)
    # The existing PointNXT callback transaction uses the durable OAuth
    # authorization-session key and performs the existing server-side exchange.
    session_store.put_auth_transaction(state, auth_session_key, {}, time.monotonic() + AUTH_CALLBACK_TIMEOUT)
    auth_bridge.begin(
        state,
        auth_session_key,
        {"redirect_uri": redirect_uri},
        time.time() + AUTH_CALLBACK_TIMEOUT,
    )
    return RedirectResponse(auth_bridge.login_url(state), status_code=302)


async def callback(request):
    from tools.auth import auth_callback

    state = request.query_params.get("state", "")
    if request.query_params.get("accessToken") or request.query_params.get("access_token"):
        return _oauth_error("invalid_request", "direct PointNXT tokens are not accepted")
    transaction = session_store.consume_oauth_transaction(state)
    if not transaction:
        return _oauth_error("invalid_grant", "authorization request is invalid or expired")
    key = transaction["auth_session_key"]
    if request.query_params.get("bridge") == "1":
        completion = session_store.consume_auth_bridge_completion(state)
        if not completion or completion["request_key"] != key:
            return _oauth_error("access_denied", "authorization was not completed", 403)
    else:
        result = await auth_callback(request)
        if getattr(result, "status_code", 500) >= 400:
            return result
    auto_approve = False
    consent_transaction = None
    token = set_request_key(key)
    try:
        session = get_session()
        if not session or not session.is_authenticated():
            return _oauth_error("access_denied", "authenticated session is unavailable", 403)
        consent_id = secrets.token_urlsafe(32)
        consent_transaction = {
            **transaction,
            "auth_session_key": key,
            "user_id": session.user_id,
            "tenant_id": session.tenant_id,
            "membership_id": session.membership_id,
            "client_display_name": await _client_display_name(
                transaction["client_id"], transaction["redirect_uri"]
            ),
        }
        session_store.put_oauth_consent_transaction(
            consent_id,
            consent_transaction,
            time.time() + MCP_OAUTH_CODE_TTL,
        )
        auto_approve = (
            transaction["client_id"] in MCP_AUTO_APPROVE_CLIENT_IDS
            and transaction["scope"] == MCP_SCOPE
        )
    finally:
        reset_request_key(token)
    if auto_approve:
        consumed = session_store.consume_oauth_consent_transaction(
            consent_id,
            client_id=consent_transaction["client_id"],
            state=consent_transaction["state"],
            redirect_uri=consent_transaction["redirect_uri"],
            resource=consent_transaction["resource"],
            scope=consent_transaction["scope"],
            auth_session_key=consent_transaction["auth_session_key"],
            user_id=consent_transaction["user_id"],
            tenant_id=consent_transaction["tenant_id"],
            membership_id=consent_transaction["membership_id"],
            decision="approved",
        )
        if not consumed:
            return _oauth_error("invalid_grant", "authorization transaction is invalid")
        return _issue_authorization_code(consumed)
    return RedirectResponse(
        f"{MCP_PUBLIC_BASE_URL}/oauth/consent?{urlencode({'consent_id': consent_id})}",
        status_code=302,
    )


def _consent_page(transaction: dict, consent_id: str) -> HTMLResponse:
    client_name = html.escape(transaction["client_display_name"], quote=True)
    consent_id_html = html.escape(consent_id, quote=True)
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Authorize PointNXT MCP</title></head>
<body>
<h1>Authorize PointNXT MCP</h1>
<p><strong>{client_name}</strong> is requesting access to PointNXT MCP.</p>
<p>Scope: <code>mcp</code></p>
<p>PointNXT MCP can read PointNXT business information including orders, customers,
products, inventory, shipments, returns, and business summaries. It can also
maintain this MCP authorization session. It does not provide commerce cancellation,
refund, or other write tools.</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="consent_id" value="{consent_id_html}">
<button type="submit" name="decision" value="approved">Approve</button>
<button type="submit" name="decision" value="denied">Deny</button>
</form>
</body>
</html>"""
    return HTMLResponse(
        body,
        headers=_with_form_action_origin(
            _CONSENT_SECURITY_HEADERS, transaction["redirect_uri"]
        ),
    )


def _session_matches_consent(transaction: dict, session) -> bool:
    return bool(
        session
        and session.is_authenticated()
        and session.user_id == transaction.get("user_id")
        and session.tenant_id == transaction.get("tenant_id")
        and session.membership_id == transaction.get("membership_id")
    )


async def consent(request):
    if not _https_request(request):
        return HTMLResponse(
            "Secure HTTPS authentication is required.",
            status_code=400,
            headers=_CONSENT_SECURITY_HEADERS,
        )
    consent_id = request.query_params.get("consent_id", "")
    transaction = session_store.get_oauth_consent_transaction(consent_id)
    if not transaction:
        return HTMLResponse(
            "Authentication request is invalid or expired.",
            status_code=400,
            headers=_CONSENT_SECURITY_HEADERS,
        )
    session = session_store.get(transaction["auth_session_key"])
    if not _session_matches_consent(transaction, session):
        return HTMLResponse("Authorization is no longer available.", status_code=403, headers=_CONSENT_SECURITY_HEADERS)
    return _consent_page(transaction, consent_id)


async def consent_decision(request):
    if not _https_request(request):
        return HTMLResponse("Secure HTTPS authentication is required.", status_code=400, headers=_CONSENT_SECURITY_HEADERS)
    values = {key: item[0] for key, item in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
    consent_id = values.get("consent_id", "")
    transaction = session_store.get_oauth_consent_transaction(consent_id)
    if not transaction:
        return HTMLResponse("Authentication request is invalid or expired.", status_code=400, headers=_CONSENT_SECURITY_HEADERS)
    session = session_store.get(transaction["auth_session_key"])
    if not _session_matches_consent(transaction, session):
        return HTMLResponse("Authorization is no longer available.", status_code=403, headers=_CONSENT_SECURITY_HEADERS)
    decision = values.get("decision", "")
    consumed = session_store.consume_oauth_consent_transaction(
        consent_id,
        client_id=transaction["client_id"],
        state=transaction["state"],
        redirect_uri=transaction["redirect_uri"],
        resource=transaction["resource"],
        scope=transaction["scope"],
        auth_session_key=transaction["auth_session_key"],
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        membership_id=session.membership_id,
        decision=decision,
    )
    if not consumed:
        return HTMLResponse("Authorization request is invalid or expired.", status_code=400, headers=_CONSENT_SECURITY_HEADERS)
    if decision == "denied":
        return RedirectResponse(
            f"{transaction['redirect_uri']}?{urlencode({'error': 'access_denied', 'error_description': 'authorization was denied', 'state': transaction['state']})}",
            status_code=302,
        )
    return _issue_authorization_code(transaction)


async def token(request):
    values = {key: item[0] for key, item in parse_qs((await request.body()).decode()).items()}
    grant_type = values.get("grant_type")
    if grant_type not in {"authorization_code", "refresh_token"}:
        return _oauth_error("unsupported_grant_type", "grant_type must be authorization_code or refresh_token")
    client_id = values.get("client_id", "")
    resource = values.get("resource", "")
    if not _resource_matches(resource):
        logger.info("OAuth token rejected: invalid resource client_id=%s redirect_uri=%s", client_id, values.get("redirect_uri", ""))
        return _oauth_error("invalid_target", "resource must identify this MCP server")
    metadata = None
    if client_id.startswith("https://"):
        try:
            metadata = await resolve_client_metadata(client_id)
        except ClientMetadataError as exc:
            logger.info("OAuth token rejected: CIMD validation failed: %s client_id=%s redirect_uri=%s", exc, client_id, values.get("redirect_uri", ""))
            return _oauth_error("invalid_client", str(exc), 401)
    if grant_type == "refresh_token":
        refresh_token = values.get("refresh_token", "")
        if not refresh_token:
            return _oauth_error("invalid_grant", "refresh_token is required")
        refresh_hash = _hash(refresh_token)
        existing = session_store.get_mcp_refresh_token(refresh_hash)
        if not existing:
            return _oauth_error("invalid_grant", "refresh_token is invalid or expired")
        session = session_store.get(existing["auth_session_key"])
        if not session or not session.is_authenticated():
            return _oauth_error("invalid_grant", "PointNXT session is no longer authenticated")
        record = session_store.consume_mcp_refresh_token(
            refresh_hash,
            client_id=client_id,
            resource=resource,
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            membership_id=session.membership_id,
        )
        if not record:
            return _oauth_error("invalid_grant", "refresh_token is invalid or has already been used")
        access_token = secrets.token_urlsafe(48)
        new_refresh_token = secrets.token_urlsafe(48)
        expires_at = time.time() + MCP_OAUTH_TOKEN_TTL
        session_store.put_mcp_token(
            _hash(access_token),
            {"auth_session_key": record["auth_session_key"], "scope": record["scope"], "resource": resource, "user_id": session.user_id, "tenant_id": session.tenant_id, "membership_id": session.membership_id},
            expires_at,
        )
        session_store.put_mcp_refresh_token(
            _hash(new_refresh_token),
            {"auth_session_key": record["auth_session_key"], "client_id": client_id, "scope": record["scope"], "resource": resource, "user_id": session.user_id, "tenant_id": session.tenant_id, "membership_id": session.membership_id},
            time.time() + MCP_REFRESH_TOKEN_TTL,
        )
        return JSONResponse({"access_token": access_token, "refresh_token": new_refresh_token, "token_type": "Bearer", "expires_in": MCP_OAUTH_TOKEN_TTL, "scope": record["scope"]})

    redirect_uri = values.get("redirect_uri", "")
    fixed_client = _client_ok(client_id, redirect_uri)
    if not fixed_client and metadata is None:
        try:
            metadata = await resolve_client_metadata(client_id)
        except ClientMetadataError:
            return _oauth_error("invalid_client", "client_id is not registered", 401)
        if metadata.get("client_id") != client_id:
            return _oauth_error("invalid_client", "client metadata client_id does not match", 401)
    if metadata is not None and redirect_uri not in metadata.get("redirect_uris", []):
        return _oauth_error("invalid_grant", "redirect_uri was not registered for this client")
    record = session_store.consume_oauth_code(_hash(values.get("code", "")))
    if (
        not record
        or record["client_id"] != client_id
        or record["redirect_uri"] != redirect_uri
        or not _resource_matches(record.get("resource", ""))
    ):
        return _oauth_error("invalid_grant", "authorization code, client_id, redirect_uri, or resource is invalid")
    verifier = values.get("code_verifier", "")
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not verifier or not secrets.compare_digest(expected, record["code_challenge"]):
        return _oauth_error("invalid_grant", "code_verifier does not match the authorization request")
    access_token = secrets.token_urlsafe(48)
    refresh_token = secrets.token_urlsafe(48)
    expires_at = time.time() + MCP_OAUTH_TOKEN_TTL
    session_store.put_mcp_token(_hash(access_token), {"auth_session_key": record["auth_session_key"], "scope": record["scope"], "resource": resource, "user_id": record["user_id"], "tenant_id": record["tenant_id"], "membership_id": record["membership_id"]}, expires_at)
    session_store.put_mcp_refresh_token(_hash(refresh_token), {"auth_session_key": record["auth_session_key"], "client_id": client_id, "scope": record["scope"], "resource": resource, "user_id": record["user_id"], "tenant_id": record["tenant_id"], "membership_id": record["membership_id"]}, time.time() + MCP_REFRESH_TOKEN_TTL)
    return JSONResponse({"access_token": access_token, "refresh_token": refresh_token, "token_type": "Bearer", "expires_in": MCP_OAUTH_TOKEN_TTL, "scope": record["scope"]})


async def revoke(request):
    values = {key: item[0] for key, item in parse_qs((await request.body()).decode()).items()}
    if values.get("token"):
        token_hash = _hash(values["token"])
        session_store.revoke_mcp_token(token_hash)
        session_store.revoke_mcp_refresh_token(token_hash)
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


def validate_bearer_credential(token: str) -> bool:
    """Validate an MCP bearer token before transport-session binding."""
    record = session_store.get_mcp_token(_hash(token))
    if not record:
        return False
    durable = session_store.get(record["auth_session_key"])
    return bool(
        durable
        and all(
            (
                record.get("resource") == MCP_RESOURCE,
                durable.user_id == record.get("user_id"),
                durable.tenant_id == record.get("tenant_id"),
                durable.membership_id == record.get("membership_id"),
                durable.is_authenticated(),
            )
        )
    )
