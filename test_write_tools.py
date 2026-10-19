"""Regression tests for the shared write-tool safety framework."""

from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

import config
from services import write_tools
from services.write_tools import (
    WriteContext,
    WriteError,
    WriteToolFramework,
    WriteToolSpec,
    register_enabled_write_tools,
)


def _spec(execute=None, name="fake_write"):
    execute = execute or AsyncMock(return_value={"ok": True})
    return WriteToolSpec(
        name=name,
        description="A test-only write tool.",
        input_schema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "Target ID."},
                "note": {"type": "string", "format": "free-text", "description": "Optional note."},
            },
            "required": ["target_id"],
        },
        destructive=True,
        open_world=False,
        load_target=lambda args, _ctx: {"id": args["target_id"], "version": "v1", "state": "OPEN"},
        validate=lambda _args, _target, _ctx: None,
        build_preview=lambda args, _target, _ctx: {
            "proposed_changes": {"note": args.get("note")},
            "summary": "Preview fake write",
        },
        execute=execute,
    )


@pytest.fixture
def enabled_writes(monkeypatch):
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ())


@pytest.fixture
def context():
    return WriteContext("user-1", "tenant-1")


@pytest.mark.asyncio
async def test_preview_does_not_execute_and_commit_requires_confirmation(enabled_writes, context):
    execute = AsyncMock(return_value={"ok": True})
    framework = WriteToolFramework(secret="x" * 32)
    spec = _spec(execute)
    preview = await framework.invoke(spec, {"target_id": "order-1"}, context)
    assert preview["status"] == "preview"
    execute.assert_not_awaited()
    with pytest.raises(WriteError, match="confirmation_token") as error:
        await framework.invoke(spec, {"target_id": "order-1", "confirm": True}, context)
    assert error.value.code == "CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_tampered_expired_user_and_tool_tokens_are_rejected(enabled_writes, context):
    framework = WriteToolFramework(secret="x" * 32)
    spec = _spec()
    preview = await framework.invoke(spec, {"target_id": "order-1"}, context)

    with pytest.raises(WriteError) as error:
        await framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"] + "x"}, context)
    assert error.value.code == "TOKEN_INVALID"

    other_user = WriteContext("user-2", "tenant-1")
    with pytest.raises(WriteError) as error:
        await framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"]}, other_user)
    assert error.value.code == "TOKEN_INVALID"

    other_tool = _spec(name="other_write")
    with pytest.raises(WriteError) as error:
        await framework.invoke(other_tool, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert error.value.code == "TOKEN_INVALID"

    expired_framework = WriteToolFramework(secret="x" * 32, ttl_seconds=-1)
    expired_preview = await expired_framework.invoke(spec, {"target_id": "order-1"}, context)
    with pytest.raises(WriteError) as error:
        await expired_framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": expired_preview["confirmation_token"]}, context)
    assert error.value.code == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_args_record_change_and_token_reuse_are_rejected(enabled_writes, context):
    version = {"value": "v1"}
    spec = _spec()
    spec = WriteToolSpec(**{**spec.__dict__, "load_target": lambda args, _ctx: {"id": args["target_id"], "version": version["value"]}})
    framework = WriteToolFramework(secret="x" * 32)
    preview = await framework.invoke(spec, {"target_id": "order-1"}, context)

    with pytest.raises(WriteError, match="Arguments changed") as error:
        await framework.invoke(spec, {"target_id": "order-2", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert error.value.code == "ARGS_CHANGED"

    version["value"] = "v2"
    with pytest.raises(WriteError) as error:
        await framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert error.value.code == "RECORD_CHANGED"

    version["value"] = "v1"
    committed = await framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert committed["status"] == "committed"
    with pytest.raises(WriteError) as error:
        await framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert error.value.code == "TOKEN_USED"


@pytest.mark.asyncio
async def test_idempotency_replays_same_result_and_rejects_changed_args(enabled_writes, context):
    execute = AsyncMock(return_value={"updated": True})
    framework = WriteToolFramework(secret="x" * 32)
    spec = _spec(execute)
    preview = await framework.invoke(spec, {"target_id": "order-1"}, context)
    args = {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"], "idempotency_key": "same-key"}
    first = await framework.invoke(spec, args, context)
    replay = await framework.invoke(spec, args, context)
    assert replay == first
    execute.assert_awaited_once()

    second_preview = await framework.invoke(spec, {"target_id": "order-1"}, context)
    with pytest.raises(WriteError) as error:
        await framework.invoke(spec, {"target_id": "order-2", "confirm": True, "confirmation_token": second_preview["confirmation_token"], "idempotency_key": "same-key"}, context)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(400, "VALIDATION_ERROR"), (401, "AUTH_REQUIRED"), (403, "NOT_PERMITTED"), (404, "NOT_FOUND"), (409, "CONFLICT"), (422, "BUSINESS_RULE_VIOLATION"), (500, "BACKEND_ERROR")])
async def test_backend_statuses_are_structured(enabled_writes, context, status, code):
    error = httpx.HTTPStatusError("backend", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(status))
    framework = WriteToolFramework(secret="x" * 32)
    spec = _spec(AsyncMock(side_effect=error))
    preview = await framework.invoke(spec, {"target_id": "order-1"}, context)
    with pytest.raises(WriteError) as raised:
        await framework.invoke(spec, {"target_id": "order-1", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_timeout_is_outcome_unknown_and_free_text_is_sanitized(enabled_writes, context):
    received = {}

    async def execute(args, _target, _ctx):
        received.update(args)
        raise TimeoutError()

    framework = WriteToolFramework(secret="x" * 32)
    spec = _spec(execute)
    preview = await framework.invoke(spec, {"target_id": "order-1", "note": "  hello\x00 world  "}, context)
    assert preview["proposed_changes"]["note"] == "hello world"
    with pytest.raises(WriteError) as error:
        await framework.invoke(spec, {"target_id": "order-1", "note": "  hello\x00 world  ", "confirm": True, "confirmation_token": preview["confirmation_token"]}, context)
    assert error.value.code == "OUTCOME_UNKNOWN"
    assert received["note"] == "hello world"


def test_write_startup_validation_requires_secret(monkeypatch):
    monkeypatch.setattr(config, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(config, "PNXT_CONFIRMATION_SECRET", "short")
    with pytest.raises(RuntimeError, match="PNXT_CONFIRMATION_SECRET"):
        config.validate_write_configuration()


def test_disabled_writes_are_not_registered_and_allowlist_is_enforced(monkeypatch):
    spec = _spec()
    server = MCPServer("test")
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", False)
    register_enabled_write_tools(server, [spec])
    assert not server._tool_manager.list_tools()

    server = MCPServer("test")
    monkeypatch.setattr(write_tools, "PNXT_ENABLE_WRITE_TOOLS", True)
    monkeypatch.setattr(write_tools, "PNXT_CONFIRMATION_SECRET", "x" * 32)
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ("other_write",))
    register_enabled_write_tools(server, [spec])
    assert not server._tool_manager.list_tools()

    server = MCPServer("test")
    monkeypatch.setattr(write_tools, "PNXT_WRITE_TOOLS_ALLOWLIST", ("fake_write",))
    register_enabled_write_tools(server, [spec])
    tool = server._tool_manager.list_tools()[0]
    assert {"confirm", "confirmation_token", "idempotency_key"} <= set(
        tool.parameters["properties"]
    )
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
