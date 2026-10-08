"""Durable session and authentication-transaction storage."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import MCP_SESSION_ENCRYPTION_KEY, MCP_SESSION_ENCRYPTION_REQUIRED

if TYPE_CHECKING:
    from services.auth_session import AuthSession

_path = os.getenv("POINTNXT_SESSION_STORE_PATH", "pointnxt-sessions.sqlite3")
_lock = RLock()
_refresh_locks: dict[str, asyncio.Lock] = {}
_SESSION_AAD = b"pnxt-mcp-auth-session-v1"


def _session_key() -> bytes | None:
    if not MCP_SESSION_ENCRYPTION_KEY:
        if MCP_SESSION_ENCRYPTION_REQUIRED:
            raise RuntimeError("MCP session encryption is not configured")
        return None
    try:
        padded = MCP_SESSION_ENCRYPTION_KEY + "=" * (-len(MCP_SESSION_ENCRYPTION_KEY) % 4)
        key = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        raise RuntimeError("MCP session encryption key is invalid") from None
    if len(key) != 32:
        raise RuntimeError("MCP session encryption key must decode to 32 bytes")
    return key


def _protect(payload: str) -> str:
    key = _session_key()
    if key is None:
        return payload
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, payload.encode(), _SESSION_AAD)
    return "enc:v1:" + base64.urlsafe_b64encode(nonce + encrypted).decode()


def _unprotect(payload: str) -> str:
    if not payload.startswith("enc:v1:"):
        if MCP_SESSION_ENCRYPTION_REQUIRED and _session_key() is None:
            raise RuntimeError("Encrypted MCP session data is required")
        return payload
    key = _session_key()
    if key is None:
        raise RuntimeError("MCP session encryption key is not configured")
    try:
        encoded = base64.urlsafe_b64decode(payload[7:])
        return AESGCM(key).decrypt(encoded[:12], encoded[12:], _SESSION_AAD).decode()
    except (InvalidTag, ValueError, TypeError):
        raise RuntimeError("MCP session data could not be decrypted") from None


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_path, timeout=10, check_same_thread=False)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS sessions (session_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_transactions (state TEXT PRIMARY KEY, request_key TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_bridge_transactions (state TEXT PRIMARY KEY, request_key TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL, nonce_hash TEXT, status TEXT NOT NULL DEFAULT 'pending')")
    connection.execute("CREATE TABLE IF NOT EXISTS refresh_leases (session_key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS oauth_transactions (state TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS oauth_codes (code_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS mcp_tokens (token_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, bound_session_key TEXT)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_rate_limits (scope TEXT PRIMARY KEY, window_start REAL NOT NULL, attempts INTEGER NOT NULL)")
    try:
        connection.execute("ALTER TABLE mcp_tokens ADD COLUMN bound_session_key TEXT")
    except sqlite3.OperationalError:
        pass
    connection.commit()
    return connection


def get(key: str | None) -> AuthSession | None:
    if not key:
        return None
    from services.auth_session import AuthSession
    with _connect() as connection:
        row = connection.execute("SELECT payload FROM sessions WHERE session_key = ?", (key,)).fetchone()
    if not row:
        return None
    values = json.loads(_unprotect(row[0]))
    for field in ("expires_at", "created_at", "last_used_at"):
        if values.get(field):
            values[field] = datetime.fromisoformat(values[field])
    session = AuthSession(**values)
    if not row[0].startswith("enc:v1:") and _session_key() is not None:
        set_(key, session)
    return session


def set_(key: str | None, session: AuthSession) -> AuthSession:
    if not key:
        raise RuntimeError("No MCP session context is available for authentication")
    payload = json.dumps({
        **session.__dict__,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_used_at": session.last_used_at.isoformat() if session.last_used_at else None,
    })
    payload = _protect(payload)
    with _connect() as connection:
        connection.execute("INSERT INTO sessions(session_key, payload, updated_at) VALUES (?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at", (key, payload, datetime.now(timezone.utc).isoformat()))
        connection.commit()
    return session


def clear(key: str | None) -> None:
    if key:
        with _connect() as connection:
            connection.execute("DELETE FROM sessions WHERE session_key = ?", (key,))
            connection.commit()
        with _lock:
            _refresh_locks.pop(key, None)


def refresh_lock(key: str | None) -> asyncio.Lock:
    if not key:
        raise RuntimeError("No MCP session context is available for token refresh")
    with _lock:
        return _refresh_locks.setdefault(key, asyncio.Lock())


def acquire_refresh_lease(key: str, timeout: float = 30, lease_seconds: float = 30) -> str:
    owner = str(uuid4())
    deadline = time.monotonic() + timeout
    while True:
        now = time.time()
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM refresh_leases WHERE session_key = ?",
                (key,),
            ).fetchone()
            if not row or row[1] <= now:
                connection.execute(
                    "INSERT INTO refresh_leases(session_key, owner, expires_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(session_key) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                    (key, owner, now + lease_seconds),
                )
                connection.commit()
                return owner
            connection.rollback()
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the session refresh lease")
        time.sleep(0.05)


def release_refresh_lease(key: str, owner: str) -> None:
    with _connect() as connection:
        connection.execute(
            "DELETE FROM refresh_leases WHERE session_key = ? AND owner = ?",
            (key, owner),
        )
        connection.commit()


def put_auth_transaction(state: str, request_key: str, payload: dict, expires_at: float) -> None:
    with _connect() as connection:
        connection.execute("INSERT OR REPLACE INTO auth_transactions(state, request_key, payload, expires_at) VALUES (?, ?, ?, ?)", (state, request_key, json.dumps(payload), expires_at))
        connection.commit()


def consume_auth_transaction(state: str) -> tuple[str, dict, float] | None:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT request_key, payload, expires_at FROM auth_transactions WHERE state = ?", (state,)).fetchone()
        if row:
            connection.execute("DELETE FROM auth_transactions WHERE state = ?", (state,))
        connection.commit()
    if not row or row[2] <= time.monotonic():
        return None
    return row[0], json.loads(row[1]), row[2]


def put_auth_bridge_transaction(
    state: str, request_key: str, payload: dict, expires_at: float
) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO auth_bridge_transactions "
            "(state, request_key, payload, expires_at, nonce_hash, status) "
            "VALUES (?, ?, ?, ?, NULL, 'pending')",
            (state, request_key, json.dumps(payload), expires_at),
        )
        connection.commit()


def get_auth_bridge_transaction(state: str) -> dict | None:
    if not state:
        return None
    with _connect() as connection:
        row = connection.execute(
            "SELECT request_key, payload, expires_at, nonce_hash, status "
            "FROM auth_bridge_transactions WHERE state = ?",
            (state,),
        ).fetchone()
    if not row or row[2] <= time.time() or row[4] == "consumed":
        return None
    return {
        "request_key": row[0],
        "payload": json.loads(row[1]),
        "expires_at": row[2],
        "nonce_hash": row[3],
        "status": row[4],
    }


def set_auth_bridge_nonce(state: str, nonce: str) -> bool:
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE auth_bridge_transactions SET nonce_hash = ? "
            "WHERE state = ? AND status = 'pending' AND expires_at > ?",
            (nonce_hash, state, time.time()),
        )
        connection.commit()
    return cursor.rowcount == 1


def consume_rate_limit(scope: str, limit: int, window_seconds: int) -> bool:
    """Atomically consume one attempt, failing closed on storage errors."""
    if limit <= 0 or window_seconds <= 0:
        return False
    now = time.time()
    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_start, attempts FROM auth_rate_limits WHERE scope = ?",
                (scope,),
            ).fetchone()
            if not row or now - row[0] >= window_seconds:
                connection.execute(
                    "INSERT INTO auth_rate_limits(scope, window_start, attempts) "
                    "VALUES (?, ?, 1) ON CONFLICT(scope) DO UPDATE SET "
                    "window_start=excluded.window_start, attempts=1",
                    (scope, now),
                )
                connection.commit()
                return True
            if row[1] >= limit:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE auth_rate_limits SET attempts = attempts + 1 WHERE scope = ?",
                (scope,),
            )
            connection.commit()
            return True
    except sqlite3.Error:
        return False


def claim_auth_bridge_transaction(state: str, nonce: str) -> dict | None:
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT request_key, payload, expires_at, nonce_hash, status "
            "FROM auth_bridge_transactions WHERE state = ?",
            (state,),
        ).fetchone()
        if (
            not row
            or row[2] <= time.time()
            or row[3] != nonce_hash
            or row[4] != "pending"
        ):
            connection.rollback()
            return None
        connection.execute(
            "UPDATE auth_bridge_transactions SET status = 'in_progress' "
            "WHERE state = ? AND status = 'pending'",
            (state,),
        )
        connection.commit()
    return {
        "request_key": row[0],
        "payload": json.loads(row[1]),
        "expires_at": row[2],
    }


def release_auth_bridge_transaction(state: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE auth_bridge_transactions SET status = 'pending' "
            "WHERE state = ? AND status = 'in_progress' AND expires_at > ?",
            (state, time.time()),
        )
        connection.commit()


def complete_auth_bridge_transaction(state: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE auth_bridge_transactions SET status = 'completed' "
            "WHERE state = ? AND status = 'in_progress' AND expires_at > ?",
            (state, time.time()),
        )
        connection.commit()
    return cursor.rowcount == 1


def consume_auth_bridge_completion(state: str) -> dict | None:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT request_key, payload, expires_at, status "
            "FROM auth_bridge_transactions WHERE state = ?",
            (state,),
        ).fetchone()
        if not row or row[2] <= time.time() or row[3] != "completed":
            connection.rollback()
            return None
        connection.execute(
            "UPDATE auth_bridge_transactions SET status = 'consumed' "
            "WHERE state = ? AND status = 'completed'",
            (state,),
        )
        connection.commit()
    return {
        "request_key": row[0],
        "payload": json.loads(row[1]),
        "expires_at": row[2],
    }


def has_auth_bridge_transaction(state: str) -> bool:
    transaction = get_auth_bridge_transaction(state)
    return bool(transaction and transaction["status"] != "consumed")


def put_oauth_transaction(state: str, payload: dict, expires_at: float) -> None:
    with _connect() as connection:
        connection.execute("INSERT OR REPLACE INTO oauth_transactions(state, payload, expires_at) VALUES (?, ?, ?)", (state, json.dumps(payload), expires_at))
        connection.commit()


def consume_oauth_transaction(state: str) -> dict | None:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload, expires_at FROM oauth_transactions WHERE state = ?", (state,)).fetchone()
        if row:
            connection.execute("DELETE FROM oauth_transactions WHERE state = ?", (state,))
        connection.commit()
    if not row or row[1] <= time.time():
        return None
    return json.loads(row[0])


def has_oauth_transaction(state: str) -> bool:
    if not state:
        return False
    with _connect() as connection:
        row = connection.execute("SELECT expires_at FROM oauth_transactions WHERE state = ?", (state,)).fetchone()
    return bool(row and row[0] > time.time())


def put_oauth_code(code_hash: str, payload: dict, expires_at: float) -> None:
    with _connect() as connection:
        connection.execute("INSERT INTO oauth_codes(code_hash, payload, expires_at) VALUES (?, ?, ?)", (code_hash, json.dumps(payload), expires_at))
        connection.commit()


def consume_oauth_code(code_hash: str) -> dict | None:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload, expires_at FROM oauth_codes WHERE code_hash = ?", (code_hash,)).fetchone()
        if row:
            connection.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (code_hash,))
        connection.commit()
    if not row or row[1] <= time.time():
        return None
    return json.loads(row[0])


def put_mcp_token(token_hash: str, payload: dict, expires_at: float) -> None:
    with _connect() as connection:
        connection.execute("INSERT OR REPLACE INTO mcp_tokens(token_hash, payload, expires_at, revoked, bound_session_key) VALUES (?, ?, ?, 0, NULL)", (token_hash, json.dumps(payload), expires_at))
        connection.commit()


def get_mcp_token(token_hash: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute("SELECT payload, expires_at, revoked FROM mcp_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    if not row or row[1] <= time.time() or row[2]:
        return None
    return json.loads(row[0])


def bind_mcp_token(token_hash: str, session_key: str) -> dict | None:
    """Atomically bind an unbound MCP token to its first transport session."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload, expires_at, revoked, bound_session_key FROM mcp_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row or row[1] <= time.time() or row[2]:
            connection.rollback()
            return None
        bound = row[3]
        if bound is None:
            connection.execute(
                "UPDATE mcp_tokens SET bound_session_key = ? WHERE token_hash = ? AND bound_session_key IS NULL",
                (session_key, token_hash),
            )
            bound = session_key
        connection.commit()
    if bound != session_key:
        return None
    return json.loads(row[0])


def revoke_mcp_token(token_hash: str) -> None:
    with _connect() as connection:
        connection.execute("UPDATE mcp_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
        connection.commit()


def clear_all_for_tests() -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM auth_transactions")
        connection.execute("DELETE FROM auth_bridge_transactions")
        connection.execute("DELETE FROM refresh_leases")
        connection.execute("DELETE FROM oauth_transactions")
        connection.execute("DELETE FROM oauth_codes")
        connection.execute("DELETE FROM mcp_tokens")
        connection.execute("DELETE FROM auth_rate_limits")
        connection.commit()
    with _lock:
        _refresh_locks.clear()
