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
:root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #15213d; background: #f7f8fa; }}
* {{ box-sizing: border-box; }}
body {{ min-height: 100vh; margin: 0; display: flex; justify-content: center; background: #f7f8fa; }}
.shell {{ width: min(100%, 620px); padding: 84px 24px 52px; text-align: center; }}
.brand {{ margin: 0 auto 39px; font-size: 25px; font-weight: 500; letter-spacing: -1.8px; line-height: 1; }}
.brand-point {{ color: #663b9d; }} .brand-nxt {{ color: #f5a623; }}
.card {{ width: min(100%, 504px); margin: 0 auto; padding: 42px 41px 38px; text-align: left; background: #fff; border: 1px solid #dfe2e8; border-radius: 18px; box-shadow: 0 12px 22px rgba(22, 31, 52, .12); }}
h1 {{ margin: 0; color: #15213d; font-size: 29px; line-height: 1.2; letter-spacing: -.8px; }}
.subcopy, .support, .footer {{ color: #737d91; font-size: 15px; }}
.subcopy {{ margin: 13px 0 30px; }}
.link {{ color: #7410c4; text-decoration: none; }}
.field {{ display: block; margin: 0 0 13px; color: #15213d; font-size: 15px; font-weight: 600; }}
.field-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 7px; }}
.field-row .field {{ margin: 0; }}
.muted-link {{ color: #737d91; font-weight: 400; text-decoration: none; }}
input[type=email], input[type=password] {{ display: block; width: 100%; height: 41px; margin-top: 7px; padding: 0 13px; border: 1px solid #d9dde5; border-radius: 9px; color: #15213d; background: #fff; font: inherit; outline: none; }}
input:focus {{ border-color: #7410c4; box-shadow: 0 0 0 3px rgba(116, 16, 196, .12); }}
.password-wrap {{ position: relative; }} .password-wrap input {{ padding-right: 44px; }}
.toggle-password {{ position: absolute; top: 50%; right: 10px; width: 28px; height: 28px; transform: translateY(-50%); border: 0; color: #737d91; background: transparent; cursor: pointer; font-size: 17px; }}
.remember {{ display: flex; align-items: center; gap: 8px; margin: 9px 0 27px; color: #737d91; font-size: 15px; }}
.remember input {{ width: 18px; height: 18px; margin: 0; accent-color: #7410c4; }}
.submit {{ display: flex; align-items: center; justify-content: center; gap: 9px; width: 100%; height: 42px; border: 0; border-radius: 9px; color: white; background: #790bc5; font: inherit; font-weight: 600; font-size: 16px; cursor: pointer; }}
.submit:hover {{ background: #6808ac; }} .submit svg {{ width: 18px; height: 18px; }}
.support {{ margin: 27px 0 0; text-align: center; }} .support a {{ color: #15213d; text-decoration: underline; text-underline-offset: 3px; }}
.footer {{ margin-top: 32px; text-align: center; font-size: 13px; }} .security {{ margin-bottom: 12px; }} .security::before {{ content: "♢"; color: #16a05d; margin-right: 8px; font-size: 17px; }}
.footer-links {{ color: #a0a7b5; }} .footer-links span {{ margin: 0 3px; }} .footer-links a {{ color: #8b93a3; text-underline-offset: 3px; }}
.error {{ margin: -10px 0 18px; padding: 10px 12px; border-radius: 8px; color: #9b1c1c; background: #fff1f1; font-size: 14px; }}
@media (max-width: 560px) {{ .shell {{ padding: 42px 16px 32px; }} .brand {{ margin-bottom: 28px; }} .card {{ padding: 30px 22px 28px; border-radius: 14px; }} h1 {{ font-size: 26px; }} }}
</style></head>
<body><main class="shell">
<div class="brand" aria-label="PointNXT"><span class="brand-point">POINT</span><span class="brand-nxt">NXT</span></div>
<section class="card" aria-labelledby="login-title">
<h1 id="login-title">Welcome back</h1>
<p class="subcopy">Don't have an account? <span class="link">Sign up</span></p>
{error_html}
<form method="post" action="/auth/login">
<input type="hidden" name="state" value="{escaped_state}">
<input type="hidden" name="nonce" value="{escaped_nonce}">
<label class="field">Email<input type="email" name="email" autocomplete="username" placeholder="name@example.com" required></label>
<div class="field-row"><label class="field" for="password">Password</label><span class="muted-link">Forgot password?</span></div>
<div class="password-wrap"><input id="password" type="password" name="password" autocomplete="current-password" required><button class="toggle-password" type="button" aria-label="Show password" data-password-toggle>⌧</button></div>
<button class="submit" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M13 5l7 7-7 7M20 12H4"/></svg>Sign in</button>
</form>
<p class="support">Trouble signing in? <span>Contact Support</span></p>
</section>
<footer class="footer"><div class="security">Protected by enterprise-grade security</div><div class="footer-links">© 2026 PointNXT <span>·</span> Terms <span>·</span> Privacy Policy <span>·</span> Data Deletion</div></footer>
</main><script>document.querySelector('[data-password-toggle]').addEventListener('click',function(){{const p=document.getElementById('password');const shown=p.type==='text';p.type=shown?'password':'text';this.textContent=shown?'⌧':'◉';this.setAttribute('aria-label',shown?'Show password':'Hide password');}});</script>
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
    return _page(state, nonce, redirect_uri=transaction.get("redirect_uri"))


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
            claimed.get("redirect_uri"),
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
