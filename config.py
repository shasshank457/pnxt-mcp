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
