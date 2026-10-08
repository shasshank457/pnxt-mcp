import os

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
MCP_OAUTH_REDIRECT_URI = os.getenv("MCP_OAUTH_REDIRECT_URI", "")
MCP_OAUTH_CODE_TTL = int(os.getenv("MCP_OAUTH_CODE_TTL", "60"))
MCP_OAUTH_TOKEN_TTL = int(os.getenv("MCP_OAUTH_TOKEN_TTL", "3600"))
MCP_CIMD_ALLOWED_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv("MCP_CIMD_ALLOWED_HOSTS", "").split(",")
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
