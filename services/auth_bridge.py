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
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def begin(state: str, request_key: str, payload: dict, expires_at: float) -> None:
    session_store.put_auth_bridge_transaction(state, request_key, payload, expires_at)


def login_url(state: str) -> str:
    return f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/auth/login?{urlencode({'state': state})}"


def _page(
    state: str,
    nonce: str,
    error: str | None = None,
    redirect_uri: str | None = None,
) -> HTMLResponse:
    escaped_state = html.escape(state, quote=True)
    escaped_nonce = html.escape(nonce, quote=True)
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in to PointNXT</title>
<style>
:root {{ color-scheme: light; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #0f172a; background: #f7f8fa; }}
*, *::before, *::after {{ box-sizing: border-box; }}
body {{ min-height: 100vh; margin: 0; display: flex; justify-content: center; background: #f7f8fa; }}
.shell {{ width: min(100%, 720px); padding: 40px 24px 48px; text-align: center; }}
.brand {{ margin: 0 auto 48px; font-size: 32px; font-weight: 700; letter-spacing: .4px; line-height: 1; }}
.brand-point {{ color: #5b2a86; }} .brand-nxt {{ color: #f7941d; }}
.card {{ width: 100%; max-width: 600px; margin: 48px auto; padding: 48px; text-align: left; background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; box-shadow: 0 4px 24px rgba(16,24,40,.06); }}
h1 {{ margin: 0 0 8px; color: #0f172a; font-size: 28px; line-height: 1.2; font-weight: 700; }}
.subcopy, .support {{ color: #64748b; font-size: 16px; }}
.subcopy {{ margin: 0; }}
.field {{ display: block; margin: 24px 0 8px; color: #0f172a; font-size: 16px; font-weight: 500; }}
.field-row {{ display: flex; justify-content: space-between; align-items: center; margin: 24px 0 8px; }}
.field-row .field {{ margin: 0; }}
.muted-link {{ color: #64748b; font-weight: 400; text-decoration: none; }}
.muted-link:hover {{ text-decoration: underline; }}
input[type=email], input[type=password] {{ display: block; width: 100%; height: 48px; margin: 0; padding: 0 16px; border: 1px solid #e2e8f0; border-radius: 8px; color: #0f172a; background: #eef2fb; font: inherit; outline: none; }}
input:focus {{ border-color: #7210b8; box-shadow: 0 0 0 3px rgba(114,16,184,.15); outline: none; }}
.password-wrap {{ position: relative; }} .password-wrap input {{ padding-right: 48px; }}
.toggle-password {{ position: absolute; top: 50%; right: 12px; display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; padding: 0; transform: translateY(-50%); border: 0; color: #64748b; background: transparent; cursor: pointer; }}
.toggle-password svg {{ width: 20px; height: 20px; }}
.submit {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; width: 100%; height: 48px; margin-top: 32px; border: 0; border-radius: 8px; color: #fff; background: #7210b8; font: inherit; font-size: 16px; font-weight: 600; cursor: pointer; }}
.submit:hover {{ background: #5f0d99; }} .submit:disabled {{ cursor: wait; opacity: .75; }} .submit svg {{ width: 20px; height: 20px; }}
.support {{ margin: 24px 0 0; text-align: center; font-size: 14px; }} .support span {{ color: #0f172a; text-decoration: underline; text-underline-offset: 3px; }}
.error {{ margin: 24px 0 0; padding: 12px; border-radius: 8px; color: #b42318; background: #fef3f2; font-size: 14px; }}
@media (max-width: 640px) {{ .shell {{ padding: 24px 16px 32px; }} .brand {{ margin-bottom: 32px; font-size: 28px; }} .card {{ margin: 32px auto; padding: 24px; }} }}
</style></head>
<body><main class="shell">
<div class="brand" aria-label="PointNXT"><span class="brand-point">POINT</span><span class="brand-nxt">NXT</span></div>
<section class="card" aria-labelledby="login-title">
<h1 id="login-title">Welcome back</h1>
<p class="subcopy">Sign in to connect PointNXT to Claude.</p>
{error_html}
<form method="post" action="/auth/login">
<input type="hidden" name="state" value="{escaped_state}">
<input type="hidden" name="nonce" value="{escaped_nonce}">
<label class="field">Email<input type="email" name="email" autocomplete="username" placeholder="name@example.com" required></label>
<div class="field-row"><label class="field" for="password">Password</label><a class="muted-link" href="https://app.pointnxt.com/login">Forgot password?</a></div>
<div class="password-wrap"><input id="password" type="password" name="password" autocomplete="current-password" required><button class="toggle-password" type="button" aria-label="Show password" data-password-toggle><svg class="eye-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M2.5 12s3.5-5 9.5-5 9.5 5 9.5 5-3.5 5-9.5 5-9.5-5-9.5-5Z"/><circle cx="12" cy="12" r="2.5"/></svg><svg class="eye-off-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true" hidden><path d="m3 3 18 18M10.6 6.2A10.8 10.8 0 0 1 12 6c6 0 9.5 6 9.5 6a16.7 16.7 0 0 1-3.1 3.7M6.7 6.8C3.9 8.2 2.5 12 2.5 12s3.5 6 9.5 6c1.1 0 2.1-.2 3-.5"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg></button></div>
<button class="submit" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M15 5l7 7-7 7M22 12H8"/><path d="M2 12h6"/></svg><span data-submit-label>Sign in</span></button>
</form>
<p class="support">Trouble signing in? <span>Contact Support</span></p>
</section>
</main><script>const form=document.querySelector('form');const toggle=document.querySelector('[data-password-toggle]');const password=document.getElementById('password');const eye=toggle.querySelector('.eye-icon');const eyeOff=toggle.querySelector('.eye-off-icon');toggle.addEventListener('click',function(){{const shown=password.type==='text';password.type=shown?'password':'text';eye.hidden=!shown;eyeOff.hidden=shown;this.setAttribute('aria-label',shown?'Show password':'Hide password');}});form.addEventListener('submit',function(){{const button=form.querySelector('.submit');button.disabled=true;form.querySelector('[data-submit-label]').textContent='Signing in…';}});</script>
</body>
</html>"""
    headers = _SECURITY_HEADERS
    if redirect_uri:
        from services.mcp_oauth import _with_form_action_origin

        headers = _with_form_action_origin(_SECURITY_HEADERS, redirect_uri)
    return HTMLResponse(body, headers=headers)


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
    return _page(
        state,
        nonce,
        redirect_uri=(transaction.get("payload") or {}).get("redirect_uri"),
    )


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
        return _page(
            state,
            next_nonce,
            "PointNXT sign-in failed.",
            (claimed.get("payload") or {}).get("redirect_uri"),
        )

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
