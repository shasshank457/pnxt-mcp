"""Focused regression tests for MCP authentication isolation."""

import asyncio
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services import session_store
from services.auth_session import get_session, set_session
from services.mcp_oauth import MCP_RESOURCE, _hash, validate_bearer
from services.mcp_session_middleware import MCPSessionMiddleware, _log_completion
from services.pointnxt_api import PointNXTAPI
from services.request_context import reset_request_key, set_request_key
from services.session_store import clear_all_for_tests


class _Connection:
    def __init__(self, session_id):
        self.session_id = session_id


async def _run_as(session_id, callback, *, http=True):
    ctx = SimpleNamespace(
        session=SimpleNamespace(_connection=_Connection(session_id)),
        request=object() if http else None,
    )
    return await MCPSessionMiddleware()(ctx, callback)


async def _run_modern_discovery():
    ctx = SimpleNamespace(
        session=SimpleNamespace(_connection=_Connection(None)),
        request=object(),
        method="server/discover",
        protocol_version="2026-07-28",
    )
    async def callback(_):
        return "discovery-ok"

    return await MCPSessionMiddleware()(ctx, callback)


async def _run_diagnostic_request():
    ctx = SimpleNamespace(
        session=SimpleNamespace(_connection=_Connection("diagnostic-session")),
        request=SimpleNamespace(
            headers={
                "mcp-protocol-version": "2025-11-25",
                "mcp-session-id": "diagnostic-session",
            }
        ),
        method="tools/list",
        protocol_version="2025-11-25",
    )

    async def callback(_):
        return {"ok": True}

    return await MCPSessionMiddleware()(ctx, callback)


class _MalformedResponse:
    status_code = 500
    headers = object()


async def main() -> None:
    clear_all_for_tests()

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("services.mcp_session_middleware")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        assert await _run_diagnostic_request() == {"ok": True}
    finally:
        handler.flush()
        logger.removeHandler(handler)
    diagnostics = stream.getvalue()
    assert "MCP incoming method=tools/list" in diagnostics
    assert "protocol=2025-11-25" in diagnostics
    assert "session_id_present=True" in diagnostics
    assert "request_header_session_present=True" in diagnostics
    assert "Authorization" not in diagnostics
    assert "diagnostic-token" not in diagnostics
    _log_completion("tools/list", _MalformedResponse())

    async def save_session(ctx):
        set_session({"data": {"accessToken": f"token-{ctx.session._connection.session_id}", "tenantId": f"tenant-{ctx.session._connection.session_id}"}})
        return get_session().as_dict()

    users = await asyncio.gather(
        _run_as("user-a", save_session),
        _run_as("user-b", save_session),
    )
    assert {user["tenant_id"] for user in users} == {"tenant-user-a", "tenant-user-b"}

    try:
        await _run_as(None, save_session)
    except RuntimeError as error:
        assert "stable MCP session identity" in str(error)
    else:
        raise AssertionError("HTTP requests without session identity must fail closed")

    # The modern 2026-07-28 discovery probe is sessionless by protocol. It is
    # allowed only for capability discovery; normal HTTP MCP requests remain
    # fail-closed without a stable transport identity.
    assert await _run_modern_discovery() == "discovery-ok"

    # Local stdio remains available for a single-process development transport.
    await _run_as(None, save_session, http=False)
    assert get_session() is None

    # Session-scoped locks are shared by separate API objects for one user and
    # distinct for different users.
    first = set_request_key("user-a")
    lock_a1 = __import__("services.session_store", fromlist=["refresh_lock"]).refresh_lock("user-a")
    reset_request_key(first)
    second = set_request_key("user-b")
    lock_b = __import__("services.session_store", fromlist=["refresh_lock"]).refresh_lock("user-b")
    reset_request_key(second)
    assert lock_a1 is not lock_b

    first = set_request_key("refresh-user")
    set_session({"data": {"accessToken": "old", "refreshToken": "refresh-old", "tenantId": "tenant-a", "expiresAt": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}})
    session = get_session()
    PointNXTAPI._update_session_tokens(
        session,
        {"expiresIn": 120},
        "new",
        "refresh-new",
    )
    persisted = session_store.get("refresh-user")
    assert persisted.get_access_token() == "new"
    assert persisted.get_refresh_token() == "refresh-new"
    assert persisted.expires_at and persisted.expires_at > datetime.now(timezone.utc)
    reset_request_key(first)

    async def lease_round():
        owner = await asyncio.to_thread(session_store.acquire_refresh_lease, "lease-user", 2, 1)
        try:
            await asyncio.sleep(0.1)
        finally:
            await asyncio.to_thread(session_store.release_refresh_lease, "lease-user", owner)

    await asyncio.gather(lease_round(), lease_round())

    auth_key = set_request_key("oauth:transaction")
    set_session({"data": {"accessToken": "oauth-token", "refreshToken": "oauth-refresh", "tenantId": "tenant-oauth", "userId": "user-oauth", "membershipId": "membership-oauth"}})
    session_store.put_mcp_token(
        _hash("mcp-token"),
        {
            "auth_session_key": "oauth:transaction",
            "scope": "mcp",
            "resource": MCP_RESOURCE,
            "user_id": "user-oauth",
            "tenant_id": "tenant-oauth",
            "membership_id": "membership-oauth",
        },
        time.time() + 60,
    )
    reset_request_key(auth_key)
    assert validate_bearer("mcp-token", "transport-a")
    assert not validate_bearer("mcp-token", "transport-b")

    clear_all_for_tests()
    print("Authentication isolation tests passed")


if __name__ == "__main__":
    asyncio.run(main())
