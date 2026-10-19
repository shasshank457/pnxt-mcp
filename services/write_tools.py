"""Shared safety framework for PointNXT write tools.

Business write tools are intentionally defined elsewhere.  This module only
provides the preview/commit protocol, confirmation tokens, idempotency, error
mapping, and registration helpers they will share.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from inspect import Parameter, Signature
from typing import Annotated, Any, Protocol
from uuid import uuid4

import httpx
from mcp_types import ToolAnnotations
from pydantic import Field

from config import (
    PNXT_CONFIRMATION_SECRET,
    PNXT_CONFIRMATION_TTL_SECONDS,
    PNXT_ENABLE_WRITE_TOOLS,
    PNXT_WRITE_FREE_TEXT_MAX_LENGTH,
    PNXT_WRITE_TIMEOUT_SECONDS,
    PNXT_WRITE_TOOLS_ALLOWLIST,
    validate_write_configuration,
)
from services.auth_session import get_session

logger = logging.getLogger(__name__)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FREE_TEXT_NAMES = frozenset({"note", "reason", "comment", "description", "message"})
_TOKEN_EXCLUDED_ARGS = frozenset({"confirm", "confirmation_token", "idempotency_key"})


class WriteError(Exception):
    """A safe, structured error returned by a write-tool handler."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.request_id = request_id or str(uuid4())

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class WriteContext:
    """Authenticated identity and request metadata supplied to a write tool."""

    user_id: str
    tenant_id: str
    request_id: str = field(default_factory=lambda: str(uuid4()))
    backend: Any = None
    timeout_seconds: int = PNXT_WRITE_TIMEOUT_SECONDS

    @classmethod
    def from_current_session(cls, backend: Any = None) -> WriteContext:
        session = get_session()
        if not session or not session.is_authenticated() or not session.tenant_id:
            raise WriteError("AUTH_REQUIRED", "Sign in to PointNXT before using write tools.")
        user_id = session.user_id or session.email
        if not user_id:
            raise WriteError("AUTH_REQUIRED", "The authenticated PointNXT user is unavailable.")
        return cls(str(user_id), str(session.tenant_id), backend=backend)


LoadTarget = Callable[[dict[str, Any], WriteContext], Any]
ValidateWrite = Callable[[dict[str, Any], Any, WriteContext], Any]
BuildPreview = Callable[[dict[str, Any], Any, WriteContext], Any]
ExecuteWrite = Callable[[dict[str, Any], Any, WriteContext], Any]


@dataclass(frozen=True)
class WriteToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    destructive: bool
    open_world: bool
    load_target: LoadTarget
    validate: ValidateWrite
    build_preview: BuildPreview
    execute: ExecuteWrite


class WriteStateStore(Protocol):
    def consume_nonce(self, nonce: str, expires_at: float) -> bool: ...

    def get_idempotency(self, user_id: str, tool: str, key: str) -> dict[str, Any] | None: ...

    def put_idempotency(
        self, user_id: str, tool: str, key: str, args_hash: str, result: dict[str, Any], expires_at: float
    ) -> None: ...


class InMemoryWriteStateStore:
    """Single-process default; the interface is deliberately replaceable by Redis."""

    def __init__(self) -> None:
        self._nonces: dict[str, float] = {}
        self._idempotency: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _purge(self, now: float) -> None:
        self._nonces = {nonce: expiry for nonce, expiry in self._nonces.items() if expiry > now}
        self._idempotency = {
            key: value
            for key, value in self._idempotency.items()
            if value["expires_at"] > now
        }

    def consume_nonce(self, nonce: str, expires_at: float) -> bool:
        now = time.time()
        self._purge(now)
        if nonce in self._nonces:
            return False
        self._nonces[nonce] = expires_at
        return True

    def get_idempotency(self, user_id: str, tool: str, key: str) -> dict[str, Any] | None:
        self._purge(time.time())
        return self._idempotency.get((user_id, tool, key))

    def put_idempotency(
        self, user_id: str, tool: str, key: str, args_hash: str, result: dict[str, Any], expires_at: float
    ) -> None:
        self._purge(time.time())
        self._idempotency[(user_id, tool, key)] = {
            "args_hash": args_hash,
            "result": result,
            "expires_at": expires_at,
        }


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _args_hash(args: dict[str, Any]) -> str:
    comparable = {key: value for key, value in args.items() if key not in _TOKEN_EXCLUDED_ARGS}
    return hashlib.sha256(_canonical(comparable).encode()).hexdigest()


class ConfirmationTokenManager:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("confirmation secret must be at least 32 bytes")
        self.secret = secret.encode()
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        spec: WriteToolSpec,
        args: dict[str, Any],
        target: Any,
        context: WriteContext,
    ) -> tuple[str, dict[str, Any]]:
        issued_at = int(time.time())
        payload = {
            "tool": spec.name,
            "user_id": context.user_id,
            "tenant_id": context.tenant_id,
            "args_hash": _args_hash(args),
            "target_id": _target_id(target),
            "target_version": _target_version(target),
            "issued_at": issued_at,
            "expires_at": issued_at + self.ttl_seconds,
            "nonce": _b64encode(uuid4().bytes),
        }
        body = _b64encode(_canonical(payload).encode())
        signature = _b64encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}", payload

    def verify(self, token: str) -> dict[str, Any]:
        try:
            body, signature = token.split(".", 1)
            expected = _b64encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                raise TypeError
            payload = json.loads(_b64decode(body))
            if not isinstance(payload, dict):
                raise TypeError
        except (ValueError, TypeError, json.JSONDecodeError):
            raise WriteError("TOKEN_INVALID", "The confirmation token is invalid.") from None
        if payload.get("expires_at", 0) <= int(time.time()):
            raise WriteError("TOKEN_EXPIRED", "The confirmation token has expired; preview again.")
        return payload


def _target_id(target: Any) -> str | None:
    return str(target.get("id")) if isinstance(target, dict) and target.get("id") else None


def _target_version(target: Any) -> str | None:
    if not isinstance(target, dict):
        return None
    value = target.get("version") or target.get("updatedAt") or target.get("updated_at")
    return str(value) if value is not None else None


def _target_summary(spec: WriteToolSpec, target: Any) -> dict[str, Any]:
    target_id = _target_id(target)
    return {
        "type": target.get("type", spec.name) if isinstance(target, dict) else spec.name,
        "id": target_id,
        "reference": target.get("reference", target_id) if isinstance(target, dict) else target_id,
    }


def _redact(value: Any, key: str = "") -> Any:
    if any(word in key.lower() for word in ("token", "secret", "password", "authorization")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {name: _redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def _sanitize_arguments(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in args or args[name] is None]
    if missing:
        raise WriteError("VALIDATION_ERROR", f"Missing required field(s): {', '.join(missing)}")
    sanitized = dict(args)
    for name, value in list(sanitized.items()):
        definition = properties.get(name, {})
        expected_type = definition.get("type")
        type_matches = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }
        if expected_type in type_matches and not type_matches[expected_type]:
            raise WriteError("VALIDATION_ERROR", f"{name} must be a {expected_type}")
        enum = definition.get("enum")
        if enum and value not in enum:
            raise WriteError("VALIDATION_ERROR", f"{name} must be one of: {', '.join(map(str, enum))}")
        if not isinstance(value, str):
            continue
        is_free_text = definition.get("format") == "free-text" or name.lower() in _FREE_TEXT_NAMES
        if not is_free_text:
            continue
        value = _CONTROL_CHARACTERS.sub("", value).strip()
        max_length = definition.get("maxLength", PNXT_WRITE_FREE_TEXT_MAX_LENGTH)
        if not value:
            raise WriteError("VALIDATION_ERROR", f"{name} must not be empty")
        if len(value) > max_length:
            raise WriteError("VALIDATION_ERROR", f"{name} must be at most {max_length} characters")
        sanitized[name] = value
    return sanitized


def _backend_status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    return status or getattr(error, "status_code", None)


def map_backend_error(error: BaseException, request_id: str) -> WriteError:
    status = _backend_status(error)
    code_by_status = {
        400: "VALIDATION_ERROR",
        401: "AUTH_REQUIRED",
        403: "NOT_PERMITTED",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "BUSINESS_RULE_VIOLATION",
    }
    if status in code_by_status:
        return WriteError(code_by_status[status], "PointNXT rejected the write request.", request_id=request_id)
    if status is not None and status >= 500:
        return WriteError("BACKEND_ERROR", "PointNXT is temporarily unavailable.", retryable=True, request_id=request_id)
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return WriteError(
            "OUTCOME_UNKNOWN",
            "The write may have reached PointNXT; check the record before retrying.",
            retryable=False,
            request_id=request_id,
        )
    return WriteError("BACKEND_ERROR", "The PointNXT write request failed.", retryable=True, request_id=request_id)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def call_authenticated_write(
    context: WriteContext,
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
) -> Any:
    """Call a backend write adapter once with the user's authenticated context.

    A future business tool supplies a backend adapter exposing ``async_request``.
    The adapter is responsible for using the current PointNXT session JWT; this
    helper supplies the bounded timeout and idempotency header and never retries.
    """
    request = getattr(context.backend, "async_request", None)
    if not callable(request):
        raise WriteError("BACKEND_ERROR", "No authenticated write backend is configured.", request_id=context.request_id)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return await request(
        method,
        endpoint,
        json=payload,
        headers=headers,
        timeout=context.timeout_seconds,
        retry=False,
    )


class WriteToolFramework:
    def __init__(
        self,
        *,
        secret: str | None = None,
        ttl_seconds: int | None = None,
        timeout_seconds: int | None = None,
        state_store: WriteStateStore | None = None,
    ) -> None:
        secret = PNXT_CONFIRMATION_SECRET if secret is None else secret
        ttl_seconds = PNXT_CONFIRMATION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        timeout_seconds = PNXT_WRITE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        self.tokens = ConfirmationTokenManager(secret, ttl_seconds)
        self.timeout_seconds = timeout_seconds
        self.state_store = state_store or InMemoryWriteStateStore()

    async def invoke(
        self,
        spec: WriteToolSpec,
        raw_args: dict[str, Any],
        context: WriteContext,
    ) -> dict[str, Any]:
        if not PNXT_ENABLE_WRITE_TOOLS:
            raise WriteError("WRITES_DISABLED", "Write tools are disabled by configuration.", request_id=context.request_id)
        if PNXT_WRITE_TOOLS_ALLOWLIST and spec.name not in PNXT_WRITE_TOOLS_ALLOWLIST:
            raise WriteError("WRITES_DISABLED", f"Write tool {spec.name!r} is not enabled.", request_id=context.request_id)

        args = _sanitize_arguments(raw_args, spec.input_schema)
        confirm = args.get("confirm", False)
        if not isinstance(confirm, bool):
            raise WriteError("VALIDATION_ERROR", "confirm must be a boolean.", request_id=context.request_id)
        target = await _maybe_await(spec.load_target(args, context))
        await _maybe_await(spec.validate(args, target, context))
        args_hash = _args_hash(args)

        if not confirm:
            preview = await _maybe_await(spec.build_preview(args, target, context))
            token, token_payload = self.tokens.issue(spec, args, target, context)
            self._audit(context, spec, "preview", target, args, None, time.perf_counter())
            return {
                "status": "preview",
                "action": spec.name,
                "target": _target_summary(spec, target),
                "current_state": target.get("state", target) if isinstance(target, dict) else target,
                "proposed_changes": preview.get("proposed_changes", preview) if isinstance(preview, dict) else preview,
                "side_effects": preview.get("side_effects", []) if isinstance(preview, dict) else [],
                "warnings": preview.get("warnings", []) if isinstance(preview, dict) else [],
                "summary": preview.get("summary", f"Preview {spec.name}") if isinstance(preview, dict) else f"Preview {spec.name}",
                "confirmation_token": token,
                "expires_at": datetime.fromtimestamp(token_payload["expires_at"], timezone.utc).isoformat(),
            }

        token = args.get("confirmation_token")
        if not isinstance(token, str) or not token:
            raise WriteError("CONFIRMATION_REQUIRED", "Set confirm=true and provide confirmation_token.", request_id=context.request_id)

        idempotency_key = args.get("idempotency_key")
        if idempotency_key:
            stored = self.state_store.get_idempotency(context.user_id, spec.name, idempotency_key)
            if stored:
                if stored["args_hash"] != args_hash:
                    raise WriteError("IDEMPOTENCY_CONFLICT", "The idempotency key was used with different arguments.", request_id=context.request_id)
                return stored["result"]

        token_payload = self.tokens.verify(token)
        if token_payload.get("tool") != spec.name or token_payload.get("user_id") != context.user_id or token_payload.get("tenant_id") != context.tenant_id:
            raise WriteError("TOKEN_INVALID", "The confirmation token does not belong to this write request.", request_id=context.request_id)
        if token_payload.get("args_hash") != args_hash:
            raise WriteError("ARGS_CHANGED", "Arguments changed since preview; request a new preview.", request_id=context.request_id)
        if token_payload.get("target_id") != _target_id(target) or token_payload.get("target_version") != _target_version(target):
            raise WriteError("RECORD_CHANGED", "The target changed since preview; request a new preview.", request_id=context.request_id)
        if not self.state_store.consume_nonce(token_payload["nonce"], token_payload["expires_at"]):
            raise WriteError("TOKEN_USED", "The confirmation token has already been used.", request_id=context.request_id)

        started = time.perf_counter()
        try:
            result = await _maybe_await(spec.execute(args, target, context))
        except WriteError:
            raise
        except Exception as error:
            mapped = map_backend_error(error, context.request_id)
            self._audit(context, spec, "commit", target, args, mapped.code, started)
            raise mapped from error

        audit_id = str(uuid4())
        committed = {"status": "committed", "result": result, "audit_id": audit_id}
        key = idempotency_key or token_payload["nonce"]
        self.state_store.put_idempotency(
            context.user_id,
            spec.name,
            key,
            args_hash,
            committed,
            time.time() + 86400,
        )
        self._audit(context, spec, "commit", target, args, None, started, audit_id)
        return committed

    @staticmethod
    def _audit(
        context: WriteContext,
        spec: WriteToolSpec,
        phase: str,
        target: Any,
        args: dict[str, Any],
        outcome: str | None,
        started: float,
        audit_id: str | None = None,
    ) -> None:
        logger.info(
            "pointnxt_write_audit",
            extra={
                "request_id": context.request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_id": context.user_id,
                "tenant_id": context.tenant_id,
                "tool": spec.name,
                "phase": phase,
                "target": _target_summary(spec, target),
                "input_args": _redact(args),
                "outcome": outcome or "success",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "audit_id": audit_id,
            },
        )


def _handler_context() -> WriteContext:
    return WriteContext.from_current_session()


def register_write_tool(mcp: Any, spec: WriteToolSpec, framework: WriteToolFramework | None = None) -> None:
    """Register one spec only when writes are enabled and allowlisted."""
    if not PNXT_ENABLE_WRITE_TOOLS:
        return
    if PNXT_WRITE_TOOLS_ALLOWLIST and spec.name not in PNXT_WRITE_TOOLS_ALLOWLIST:
        return
    framework = framework or WriteToolFramework()

    async def handler(**kwargs: Any) -> dict[str, Any]:
        try:
            return await framework.invoke(spec, kwargs, _handler_context())
        except WriteError as error:
            return error.as_dict()

    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    # The MCP SDK derives schemas from signatures.  Keep the business schema
    # authoritative while adding the safety controls automatically.
    properties = dict(spec.input_schema.get("properties", {}))
    properties.update({
        "confirm": {"type": "boolean", "default": False, "description": "Commit the previously previewed write."},
        "confirmation_token": {"type": "string", "description": "Token returned by the preview."},
        "idempotency_key": {"type": "string", "description": "Optional key used to safely retry the same commit."},
    })
    parameters = []
    required = set(spec.input_schema.get("required", []))
    for name, definition in properties.items():
        description = definition.get("description", f"Write-tool parameter {name}.")
        annotation = Annotated[Any, Field(description=description)]
        default = Parameter.empty if name in required else definition.get("default", None)
        parameters.append(
            Parameter(name, Parameter.KEYWORD_ONLY, annotation=annotation, default=default)
        )
    handler.__signature__ = Signature(parameters)
    annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=spec.destructive,
        idempotentHint=False,
        openWorldHint=spec.open_world,
    )
    mcp.add_tool(handler, name=spec.name, description=spec.description, annotations=annotations)


def register_enabled_write_tools(mcp: Any, specs: list[WriteToolSpec] | tuple[WriteToolSpec, ...] = ()) -> None:
    """Validate configuration and register the supplied future business specs."""
    validate_write_configuration()
    if not PNXT_ENABLE_WRITE_TOOLS:
        return
    for spec in specs:
        register_write_tool(mcp, spec)


__all__ = [
    "BuildPreview",
    "ExecuteWrite",
    "InMemoryWriteStateStore",
    "LoadTarget",
    "ValidateWrite",
    "WriteContext",
    "WriteError",
    "WriteToolFramework",
    "WriteToolSpec",
    "call_authenticated_write",
    "map_backend_error",
    "register_enabled_write_tools",
    "register_write_tool",
]
