import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

POINTNXT_BASE_URL = os.getenv("POINTNXT_BASE_URL")
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
POINTNXT_LOGIN_URL = os.getenv("POINTNXT_LOGIN_URL", "https://app.pointnxt.com/login")
POINTNXT_AUTH_CALLBACK_URL = os.getenv("POINTNXT_AUTH_CALLBACK_URL", "")
POINTNXT_ACCESS_TOKEN = os.getenv("POINTNXT_ACCESS_TOKEN")
POINTNXT_REFRESH_TOKEN = os.getenv("POINTNXT_REFRESH_TOKEN")
POINTNXT_REFRESH_ENDPOINT = os.getenv(
    "POINTNXT_REFRESH_ENDPOINT", "/auth/refresh-token"
)
POINTNXT_TENANT_ID = os.getenv("POINTNXT_TENANT_ID")
AUTH_CALLBACK_TIMEOUT = int(os.getenv("AUTH_CALLBACK_TIMEOUT", "180"))
MCP_PUBLIC_BASE_URL = os.getenv("MCP_PUBLIC_BASE_URL", "https://mcp.pointnxt.com")
MCP_OAUTH_CLIENT_ID = os.getenv("MCP_OAUTH_CLIENT_ID", "claude")
MCP_AUTO_APPROVE_CLIENT_IDS = tuple(
    client_id.strip()
    for client_id in os.getenv("MCP_AUTO_APPROVE_CLIENT_IDS", "").split(",")
    if client_id.strip()
)
MCP_OAUTH_REDIRECT_URI = os.getenv("MCP_OAUTH_REDIRECT_URI", "")
OAUTH_ALLOWED_REDIRECT_URIS = tuple(
    uri.strip()
    for uri in os.getenv(
        "OAUTH_ALLOWED_REDIRECT_URIS",
        "https://claude.ai/api/mcp/auth_callback,"
        "https://claude.com/api/mcp/auth_callback,"
        "https://chatgpt.com/connector_platform_oauth_redirect",
    ).split(",")
    if uri.strip()
)
CHATGPT_REDIRECT_URI = os.getenv("CHATGPT_REDIRECT_URI", "").strip()
MCP_OAUTH_CODE_TTL = int(os.getenv("MCP_OAUTH_CODE_TTL", "60"))
MCP_OAUTH_TOKEN_TTL = int(os.getenv("MCP_OAUTH_TOKEN_TTL", "3600"))
MCP_CIMD_ALLOWED_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv(
        "CIMD_ALLOWED_HOSTS",
        os.getenv("MCP_CIMD_ALLOWED_HOSTS", "chatgpt.com,claude.ai,claude.com"),
    ).split(",")
    if host.strip()
)
MCP_AUTH_RATE_LIMIT_IP = int(os.getenv("MCP_AUTH_RATE_LIMIT_IP", "10"))
MCP_AUTH_RATE_LIMIT_TRANSACTION = int(
    os.getenv("MCP_AUTH_RATE_LIMIT_TRANSACTION", "5")
)
MCP_AUTH_RATE_LIMIT_WINDOW = int(os.getenv("MCP_AUTH_RATE_LIMIT_WINDOW", "300"))
MCP_SESSION_ENCRYPTION_KEY = os.getenv("MCP_SESSION_ENCRYPTION_KEY", "")
MCP_SESSION_ENCRYPTION_REQUIRED = os.getenv(
    "MCP_SESSION_ENCRYPTION_REQUIRED", "false" if DEV_MODE else "true"
).lower() == "true"
# Comma-separated proxy IPs, networks, or trusted UNIX socket paths.  An empty
# value deliberately trusts no forwarded headers; deployments behind TLS
# proxies must configure this explicitly.
MCP_FORWARDED_ALLOW_IPS = os.getenv("MCP_FORWARDED_ALLOW_IPS", "")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
PNXT_ENABLE_WRITE_TOOLS = os.getenv("PNXT_ENABLE_WRITE_TOOLS", "false").lower() == "true"
PNXT_WRITE_TOOLS_ALLOWLIST = tuple(
    name.strip()
    for name in os.getenv("PNXT_WRITE_TOOLS_ALLOWLIST", "").split(",")
    if name.strip()
)
PNXT_CONFIRMATION_SECRET = os.getenv("PNXT_CONFIRMATION_SECRET", "")
PNXT_CONFIRMATION_TTL_SECONDS = int(os.getenv("PNXT_CONFIRMATION_TTL_SECONDS", "300"))
PNXT_WRITE_TIMEOUT_SECONDS = int(os.getenv("PNXT_WRITE_TIMEOUT_SECONDS", "30"))
PNXT_WRITE_FREE_TEXT_MAX_LENGTH = int(
    os.getenv("PNXT_WRITE_FREE_TEXT_MAX_LENGTH", "1000")
)


def validate_mcp_oauth_configuration() -> None:
    """Validate the optional fixed-client callback at startup."""
    callback = MCP_OAUTH_REDIRECT_URI
    if not callback:
        return
    if callback != callback.strip():
        raise RuntimeError("MCP_OAUTH_REDIRECT_URI must not contain surrounding whitespace")
    try:
        parsed = urlsplit(callback)
    except ValueError as exc:
        raise RuntimeError("MCP_OAUTH_REDIRECT_URI is not a valid callback URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError(
            "MCP_OAUTH_REDIRECT_URI must be an HTTPS callback URL without credentials or fragments"
        )


def validate_write_configuration() -> None:
    """Fail closed when the optional write framework is enabled incorrectly."""
    if not PNXT_ENABLE_WRITE_TOOLS:
        return
    if len(PNXT_CONFIRMATION_SECRET.encode()) < 32:
        raise RuntimeError(
            "PNXT_CONFIRMATION_SECRET must be configured with at least 32 bytes "
            "when PNXT_ENABLE_WRITE_TOOLS=true"
        )
    if PNXT_CONFIRMATION_TTL_SECONDS <= 0:
        raise RuntimeError("PNXT_CONFIRMATION_TTL_SECONDS must be positive")
    if PNXT_WRITE_TIMEOUT_SECONDS <= 0:
        raise RuntimeError("PNXT_WRITE_TIMEOUT_SECONDS must be positive")
    if PNXT_WRITE_FREE_TEXT_MAX_LENGTH <= 0:
        raise RuntimeError("PNXT_WRITE_FREE_TEXT_MAX_LENGTH must be positive")
