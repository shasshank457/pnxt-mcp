import asyncio
import json
import logging
from datetime import datetime, timezone
from time import perf_counter, sleep
from uuid import uuid4

import httpx
import requests

from config import (
    DEV_MODE,
    POINTNXT_ACCESS_TOKEN,
    POINTNXT_BASE_URL,
    POINTNXT_REFRESH_ENDPOINT,
    POINTNXT_REFRESH_TOKEN,
    POINTNXT_TENANT_ID,
)
from services.auth_session import clear_session, get_session
from services.metrics import record_request

logger = logging.getLogger(__name__)


def _structured_log(level: int, event: str, **fields) -> None:
    """Write a JSON-formatted log entry without including credentials."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    logger.log(level, json.dumps(payload, separators=(",", ":"), default=str))


class PointNXTAPI:
    def __init__(self):
        self.base_url = (POINTNXT_BASE_URL or "").rstrip("/")

        access_token = (POINTNXT_ACCESS_TOKEN or "").strip()
        self.refresh_token = (POINTNXT_REFRESH_TOKEN or "").strip()
        self.refresh_endpoint = POINTNXT_REFRESH_ENDPOINT or "/auth/refresh-token"
        tenant_id = (POINTNXT_TENANT_ID or "").strip()

        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "x-tenant-id": tenant_id,
            "Content-Type": "application/json",
        }
        self.dev_access_token = access_token
        self.dev_tenant_id = tenant_id
        self._refresh_lock = asyncio.Lock()

    def _apply_auth(self) -> None:
        session = get_session()
        if session and session.is_authenticated():
            logger.info("PointNXT authentication session loaded for API request")
            token, tenant = session.get_access_token(), session.get_tenant_id()
        elif DEV_MODE and self.dev_access_token and self.dev_tenant_id:
            token, tenant = self.dev_access_token, self.dev_tenant_id
        else:
            raise RuntimeError("Please sign in to PointNXT first.")
        self.headers.update({"Authorization": f"Bearer {token}", "x-tenant-id": tenant})

    def check_backend_health(self) -> dict:
        """Check whether the configured PointNXT backend is reachable."""
        started_at = perf_counter()
        try:
            response = requests.get(
                self.base_url,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            latency_ms = round((perf_counter() - started_at) * 1000, 2)
            return {
                "status": "healthy",
                "reachable": True,
                "http_status": response.status_code,
                "latency_ms": latency_ms,
            }
        except requests.Timeout:
            return {
                "status": "unhealthy",
                "reachable": False,
                "error": "Backend health check timed out",
                "latency_ms": round((perf_counter() - started_at) * 1000, 2),
            }
        except requests.RequestException as error:
            return {
                "status": "unhealthy",
                "reachable": False,
                "error": f"Backend is unreachable: {error}",
                "latency_ms": round((perf_counter() - started_at) * 1000, 2),
            }

    def check_authentication(self) -> dict:
        """Validate authentication with a lightweight authenticated request."""
        session = get_session()
        if not session or not session.is_authenticated():
            return {
                "status": "unhealthy",
                "authenticated": False,
                "tenant_id_present": bool(session and session.get_tenant_id()),
                "expires_at": session.expires_at.isoformat()
                if session and session.expires_at
                else None,
                "authentication_mode": "none",
                "error": "Please sign in to PointNXT first.",
            }
        started_at = perf_counter()
        try:
            self.get("/commerce/orders", params={"limit": 1, "page": 1})
            return {
                "status": "healthy",
                "authenticated": True,
                "tenant_id_present": bool(session.get_tenant_id()),
                "expires_at": session.expires_at.isoformat()
                if session.expires_at
                else None,
                "authentication_mode": "browser_session",
                "latency_ms": round((perf_counter() - started_at) * 1000, 2),
            }
        except Exception as error:  # noqa: BLE001 - diagnostics must normalize all client failures
            return {
                "status": "unhealthy",
                "authenticated": False,
                "error": str(error),
                "latency_ms": round((perf_counter() - started_at) * 1000, 2),
            }

    def check_configuration(self) -> dict:
        """Check required API configuration without making a network request."""
        session = get_session()
        session_active = bool(session and session.is_authenticated())
        dev_configured = DEV_MODE and bool(self.dev_access_token and self.dev_tenant_id)
        configured = {
            "base_url": bool(self.base_url),
            "session": session_active or dev_configured,
        }
        missing = [name for name, value in configured.items() if not value]
        return {
            "status": "healthy" if not missing else "unhealthy",
            "configured": not missing,
            "required": {
                "base_url": configured["base_url"],
                "authenticated_session": configured["session"],
            },
            "authenticated_session": configured["session"],
            "authentication_mode": "browser_session"
            if session_active
            else ("dev_service_token" if dev_configured else "none"),
            "tenant_id_present": bool(session and session.get_tenant_id())
            if session_active
            else bool(dev_configured),
            "expires_at": session.expires_at.isoformat()
            if session and session.expires_at
            else None,
            "missing": missing,
        }

    def _refresh_access_token(self) -> None:
        """Refresh the access token and update the shared auth headers."""
        session = get_session()
        refresh_token = session.get_refresh_token() if session else self.refresh_token
        if not refresh_token:
            raise RuntimeError(
                "PointNXT authentication failed: no refresh token is configured. "
                "Set POINTNXT_REFRESH_TOKEN in the environment."
            )

        request_id = str(uuid4())
        started_at = perf_counter()
        method = "POST"
        endpoint = self.refresh_endpoint
        try:
            response = requests.post(
                f"{self.base_url}{self.refresh_endpoint}",
                json={"refreshToken": refresh_token},
                headers={
                    "x-tenant-id": self.headers["x-tenant-id"],
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            _structured_log(
                logging.ERROR,
                "pointnxt_request_error",
                request_id=request_id,
                method=method,
                endpoint=endpoint,
                tenant_id=self.headers["x-tenant-id"],
                error="token refresh timed out",
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise RuntimeError(
                "PointNXT authentication failed: token refresh timed out"
            ) from error
        except requests.ConnectionError as error:
            _structured_log(
                logging.ERROR,
                "pointnxt_request_error",
                request_id=request_id,
                method=method,
                endpoint=endpoint,
                tenant_id=self.headers["x-tenant-id"],
                error="token refresh connection failure",
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise RuntimeError(
                "PointNXT authentication failed: unable to connect during token refresh"
            ) from error
        except (requests.HTTPError, ValueError) as error:
            _structured_log(
                logging.ERROR,
                "pointnxt_request_error",
                request_id=request_id,
                method=method,
                endpoint=endpoint,
                tenant_id=self.headers["x-tenant-id"],
                status_code=getattr(response, "status_code", None),
                error="token refresh rejected or returned invalid JSON",
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise RuntimeError(
                "PointNXT authentication failed: token refresh was rejected"
            ) from error

        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        access_token = data.get("accessToken") or data.get("access_token")
        refreshed_token = data.get("refreshToken") or data.get("refresh_token")
        if not access_token:
            raise RuntimeError(
                "PointNXT authentication failed: refresh response did not contain "
                "a new access token"
            )

        self.headers["Authorization"] = f"Bearer {access_token}"
        if session:
            session.access_token = access_token
            if refreshed_token:
                session.refresh_token = refreshed_token
        if refreshed_token:
            self.refresh_token = refreshed_token
        _structured_log(
            logging.INFO,
            "pointnxt_token_refresh_succeeded",
            request_id=request_id,
            method=method,
            endpoint=endpoint,
            tenant_id=self.headers["x-tenant-id"],
            status_code=response.status_code,
            latency_ms=round((perf_counter() - started_at) * 1000, 2),
        )

    def _request(
        self, method: str, endpoint: str, _retry_after_refresh: bool = True, **kwargs
    ) -> dict:
        """Send an authenticated request and return its JSON response."""

        self._apply_auth()
        params = kwargs.get("params")
        log_params = params if params else {}
        kwargs.setdefault("timeout", 30)
        max_attempts = 3
        retryable_statuses = {502, 503, 504}

        for attempt in range(1, max_attempts + 1):
            request_id = str(uuid4())
            _structured_log(
                logging.INFO,
                "pointnxt_request_started",
                request_id=request_id,
                method=method,
                endpoint=endpoint,
                tenant_id=self.headers["x-tenant-id"],
                params=log_params,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            started_at = perf_counter()

            try:
                response = requests.request(
                    method,
                    f"{self.base_url}{endpoint}",
                    headers=self.headers,
                    **kwargs,
                )
            except requests.Timeout as error:
                elapsed = perf_counter() - started_at
                if attempt < max_attempts:
                    delay = 2 ** (attempt - 1)
                    _structured_log(
                        logging.WARNING,
                        "pointnxt_request_retry",
                        request_id=request_id,
                        method=method,
                        endpoint=endpoint,
                        tenant_id=self.headers["x-tenant-id"],
                        error="timeout",
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        delay_seconds=delay,
                        latency_ms=round((perf_counter() - started_at) * 1000, 2),
                    )
                    sleep(delay)
                    continue
                _structured_log(
                    logging.ERROR,
                    "pointnxt_request_error",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    error="timeout",
                    latency_ms=round(elapsed * 1000, 2),
                )
                raise RuntimeError("PointNXT API request timed out") from error
            except requests.ConnectionError as error:
                elapsed = perf_counter() - started_at
                if attempt < max_attempts:
                    delay = 2 ** (attempt - 1)
                    _structured_log(
                        logging.WARNING,
                        "pointnxt_request_retry",
                        request_id=request_id,
                        method=method,
                        endpoint=endpoint,
                        tenant_id=self.headers["x-tenant-id"],
                        error="connection_failure",
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        delay_seconds=delay,
                        latency_ms=round((perf_counter() - started_at) * 1000, 2),
                    )
                    sleep(delay)
                    continue
                _structured_log(
                    logging.ERROR,
                    "pointnxt_request_error",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    error="connection_failure",
                    latency_ms=round(elapsed * 1000, 2),
                )
                raise RuntimeError("Unable to connect to the PointNXT API") from error

            elapsed = perf_counter() - started_at
            _structured_log(
                logging.INFO,
                "pointnxt_request_completed",
                request_id=request_id,
                method=method,
                endpoint=endpoint,
                tenant_id=self.headers["x-tenant-id"],
                status_code=response.status_code,
                latency_ms=round(elapsed * 1000, 2),
                attempt=attempt,
            )

            if response.status_code == 401 and _retry_after_refresh:
                _structured_log(
                    logging.WARNING,
                    "pointnxt_auth_refresh_attempt",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    status_code=response.status_code,
                    latency_ms=round(elapsed * 1000, 2),
                )
                try:
                    self._refresh_access_token()
                except Exception:  # noqa: BLE001 - refresh failures become auth-required errors
                    clear_session()
                    raise RuntimeError("Please sign in to PointNXT first.")
                return self._request(
                    method, endpoint, _retry_after_refresh=False, **kwargs
                )

            if response.status_code in retryable_statuses and attempt < max_attempts:
                delay = 2 ** (attempt - 1)
                _structured_log(
                    logging.WARNING,
                    "pointnxt_request_retry",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    status_code=response.status_code,
                    attempt=attempt,
                    next_attempt=attempt + 1,
                    delay_seconds=delay,
                    latency_ms=round(elapsed * 1000, 2),
                )
                sleep(delay)
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError:
                _structured_log(
                    logging.ERROR,
                    "pointnxt_request_error",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    status_code=response.status_code,
                    error="http_error",
                    latency_ms=round(elapsed * 1000, 2),
                )
                raise
            return response.json()

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            result = self._request("GET", endpoint, params=params)
            record_request(
                request_id=request_id,
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
                backend_latency_ms=round((perf_counter() - started_at) * 1000, 2),
                success=True,
            )
            return result
        except Exception:
            elapsed = round((perf_counter() - started_at) * 1000, 2)
            record_request(
                request_id=request_id,
                latency_ms=elapsed,
                backend_latency_ms=elapsed,
                success=False,
            )
            raise

    async def async_get(self, endpoint: str, params: dict | None = None) -> dict:
        """Run a native async authenticated GET with retry support."""
        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            result = await self._async_request("GET", endpoint, params=params)
            record_request(
                request_id=request_id,
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
                backend_latency_ms=round((perf_counter() - started_at) * 1000, 2),
                success=True,
            )
            return result
        except Exception:
            elapsed = round((perf_counter() - started_at) * 1000, 2)
            record_request(
                request_id=request_id,
                latency_ms=elapsed,
                backend_latency_ms=elapsed,
                success=False,
            )
            raise

    async def _async_refresh_access_token(self, client: httpx.AsyncClient) -> None:
        session = get_session()
        refresh_token = session.get_refresh_token() if session else self.refresh_token
        if not refresh_token:
            raise RuntimeError(
                "PointNXT authentication failed: no refresh token is configured. "
                "Set POINTNXT_REFRESH_TOKEN in the environment."
            )

        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            response = await client.post(
                f"{self.base_url}{self.refresh_endpoint}",
                json={"refreshToken": refresh_token},
                headers={
                    "x-tenant-id": self.headers["x-tenant-id"],
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.HTTPError,
            ValueError,
        ) as error:
            _structured_log(
                logging.ERROR,
                "pointnxt_request_error",
                request_id=request_id,
                method="POST",
                endpoint=self.refresh_endpoint,
                tenant_id=self.headers["x-tenant-id"],
                status_code=getattr(response, "status_code", None)
                if "response" in locals()
                else None,
                error="async token refresh failed",
                latency_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            raise RuntimeError(
                "PointNXT authentication failed: token refresh was rejected"
            ) from error

        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        access_token = data.get("accessToken") or data.get("access_token")
        refreshed_token = data.get("refreshToken") or data.get("refresh_token")
        if not access_token:
            raise RuntimeError(
                "PointNXT authentication failed: refresh response did not contain "
                "a new access token"
            )
        self.headers["Authorization"] = f"Bearer {access_token}"
        if session:
            session.access_token = access_token
            if refreshed_token:
                session.refresh_token = refreshed_token
        if refreshed_token:
            self.refresh_token = refreshed_token

        _structured_log(
            logging.INFO,
            "pointnxt_token_refresh_succeeded",
            request_id=request_id,
            method="POST",
            endpoint=self.refresh_endpoint,
            tenant_id=self.headers["x-tenant-id"],
            status_code=response.status_code,
            latency_ms=round((perf_counter() - started_at) * 1000, 2),
        )

    async def _async_request(
        self, method: str, endpoint: str, _retry_after_refresh: bool = True, **kwargs
    ) -> dict:
        max_attempts = 3
        retryable_statuses = {502, 503, 504}
        timeout = kwargs.pop("timeout", 30)

        async with httpx.AsyncClient(timeout=timeout) as client:
            session = get_session()
            if session and session.is_expired() and session.get_refresh_token():
                try:
                    await self._async_refresh_access_token(client)
                except Exception:  # noqa: BLE001 - refresh failures become auth-required errors
                    clear_session()
                    raise RuntimeError("Please sign in to PointNXT first.")
            self._apply_auth()
            for attempt in range(1, max_attempts + 1):
                request_id = str(uuid4())
                started_at = perf_counter()
                _structured_log(
                    logging.INFO,
                    "pointnxt_request_started",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    params=kwargs.get("params", {}),
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                try:
                    response = await client.request(
                        method,
                        f"{self.base_url}{endpoint}",
                        headers=self.headers,
                        **kwargs,
                    )
                except (httpx.TimeoutException, httpx.ConnectError) as error:
                    latency_ms = round((perf_counter() - started_at) * 1000, 2)
                    if attempt < max_attempts:
                        delay = 2 ** (attempt - 1)
                        _structured_log(
                            logging.WARNING,
                            "pointnxt_request_retry",
                            request_id=request_id,
                            method=method,
                            endpoint=endpoint,
                            tenant_id=self.headers["x-tenant-id"],
                            error=type(error).__name__,
                            attempt=attempt,
                            next_attempt=attempt + 1,
                            delay_seconds=delay,
                            latency_ms=latency_ms,
                        )
                        await asyncio.sleep(delay)
                        continue
                    _structured_log(
                        logging.ERROR,
                        "pointnxt_request_error",
                        request_id=request_id,
                        method=method,
                        endpoint=endpoint,
                        tenant_id=self.headers["x-tenant-id"],
                        error=type(error).__name__,
                        latency_ms=latency_ms,
                    )
                    raise RuntimeError("PointNXT API request failed") from error

                latency_ms = round((perf_counter() - started_at) * 1000, 2)
                _structured_log(
                    logging.INFO,
                    "pointnxt_request_completed",
                    request_id=request_id,
                    method=method,
                    endpoint=endpoint,
                    tenant_id=self.headers["x-tenant-id"],
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    attempt=attempt,
                )

                if response.status_code == 401 and _retry_after_refresh:
                    session_before = get_session()
                    token_before = (
                        session_before.get_access_token() if session_before else None
                    )
                    async with self._refresh_lock:
                        session_after = get_session()
                        if (
                            not session_after
                            or session_after.get_access_token() == token_before
                        ):
                            try:
                                await self._async_refresh_access_token(client)
                            except Exception:  # noqa: BLE001 - refresh failures become auth-required errors
                                clear_session()
                                raise RuntimeError("Please sign in to PointNXT first.")
                    return await self._async_request(
                        method, endpoint, _retry_after_refresh=False, **kwargs
                    )

                if (
                    response.status_code in retryable_statuses
                    and attempt < max_attempts
                ):
                    delay = 2 ** (attempt - 1)
                    _structured_log(
                        logging.WARNING,
                        "pointnxt_request_retry",
                        request_id=request_id,
                        method=method,
                        endpoint=endpoint,
                        tenant_id=self.headers["x-tenant-id"],
                        status_code=response.status_code,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        delay_seconds=delay,
                        latency_ms=latency_ms,
                    )
                    await asyncio.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()

    def post(self, endpoint: str, payload: dict) -> dict:
        return self._request("POST", endpoint, json=payload)

    def put(self, endpoint: str, payload: dict) -> dict:
        return self._request("PUT", endpoint, json=payload)

    def delete(self, endpoint: str) -> dict:
        return self._request("DELETE", endpoint)
