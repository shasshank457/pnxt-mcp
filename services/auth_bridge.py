"""External PointNXT login bridge for the MCP authorization flow."""

import html
import logging
import secrets
from urllib.parse import parse_qs, urlencode

from starlette.responses import HTMLResponse, RedirectResponse

from config import (
    MCP_AUTH_RATE_LIMIT_IP,
    MCP_AUTH_RATE_LIMIT_TRANSACTION,
    MCP_AUTH_RATE_LIMIT_WINDOW,
    MCP_PUBLIC_BASE_URL,
)
from services import session_store
from services.request_context import reset_request_key, set_request_key

logger = logging.getLogger(__name__)
_MAX_LOGIN_BODY = 16 * 1024
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def begin(state: str, request_key: str, payload: dict, expires_at: float) -> None:
    session_store.put_auth_bridge_transaction(state, request_key, payload, expires_at)


def login_url(state: str) -> str:
    return f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/auth/login?{urlencode({'state': state})}"


def _page(state: str, nonce: str, error: str | None = None) -> HTMLResponse:
    escaped_state = html.escape(state, quote=True)
    escaped_nonce = html.escape(nonce, quote=True)
    error_html = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Sign in to PointNXT</title></head>
<body>
<h1>Sign in to PointNXT</h1>
{error_html}
<form method="post" action="/auth/login">
<input type="hidden" name="state" value="{escaped_state}">
<input type="hidden" name="nonce" value="{escaped_nonce}">
<label>Email <input type="email" name="email" autocomplete="username" required></label>
<label>Password <input type="password" name="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button>
</form>
</body>
</html>"""
    return HTMLResponse(
        body,
        headers=_SECURITY_HEADERS,
    )


def _error(message: str, status_code: int = 400) -> HTMLResponse:
    return HTMLResponse(message, status_code=status_code, headers=_SECURITY_HEADERS)


def _is_https(request) -> bool:
    return getattr(getattr(request, "url", None), "scheme", "") == "https"


async def _read_login_body(request) -> bytes | None:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_LOGIN_BODY:
                return None
        except ValueError:
            return None
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_LOGIN_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def login_page(request):
    if not _is_https(request):
        return _error("Secure HTTPS authentication is required.", status_code=400)
    state = request.query_params.get("state", "")
    transaction = session_store.get_auth_bridge_transaction(state)
    if not transaction:
        return _error("Authentication request is invalid or expired.")
    nonce = secrets.token_urlsafe(32)
    if not session_store.set_auth_bridge_nonce(state, nonce):
        return _error("Authentication request is invalid or expired.")
    return _page(state, nonce)


async def login_submit(request):
    if not _is_https(request):
        return _error("Secure HTTPS authentication is required.", status_code=400)
    raw_body = await _read_login_body(request)
    if raw_body is None:
        return _error("Authentication request is too large.")
    try:
        values = {
            key: item[0]
            for key, item in parse_qs(raw_body.decode("utf-8"), keep_blank_values=True).items()
        }
    except UnicodeDecodeError:
        return _error("Authentication request is invalid.")
    state = values.get("state", "")
    nonce = values.get("nonce", "")
    email = values.get("email", "")
    password = values.get("password", "")
    if not state or not nonce or not email or not password:
        return _error("Email and password are required.")

    client_host = request.client.host if request.client else "unknown"
    ip_allowed = session_store.consume_rate_limit(
        f"auth-login-ip:{client_host}",
        MCP_AUTH_RATE_LIMIT_IP,
        MCP_AUTH_RATE_LIMIT_WINDOW,
    )
    transaction_allowed = session_store.consume_rate_limit(
        f"auth-login-state:{state}",
        MCP_AUTH_RATE_LIMIT_TRANSACTION,
        MCP_AUTH_RATE_LIMIT_WINDOW,
    )
    if not ip_allowed or not transaction_allowed:
        return _error("Too many sign-in attempts. Try again later.", status_code=429)

    claimed = session_store.claim_auth_bridge_transaction(state, nonce)
    if not claimed:
        return _error("Authentication request is invalid or expired.")

    from tools.auth import login_with_credentials

    token = set_request_key(claimed["request_key"])
    try:
        try:
            result = await login_with_credentials(email, password)
        except Exception:  # noqa: BLE001 - normalize bridge authentication failures
            logger.warning("PointNXT bridge authentication failed")
            result = {"authenticated": False}
    finally:
        reset_request_key(token)

    if not result.get("authenticated"):
        session_store.clear(claimed["request_key"])
        session_store.release_auth_bridge_transaction(state)
        next_nonce = secrets.token_urlsafe(32)
        session_store.set_auth_bridge_nonce(state, next_nonce)
        return _page(state, next_nonce, "PointNXT sign-in failed.")

    if not session_store.complete_auth_bridge_transaction(state):
        session_store.clear(claimed["request_key"])
        return _error("Authentication request is invalid or expired.")
    callback = f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/auth/callback"
    return RedirectResponse(
        f"{callback}?{urlencode({'state': state, 'bridge': '1'})}",
        status_code=303,
    )


async def callback(request):
    state = request.query_params.get("state", "")
    completion = session_store.consume_auth_bridge_completion(state)
    if not completion:
        return HTMLResponse("Authentication request is invalid or expired.", status_code=400)
    return HTMLResponse(
        "PointNXT sign-in complete. You may close this window.",
        headers=_SECURITY_HEADERS,
    )
