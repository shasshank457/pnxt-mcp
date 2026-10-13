"""Regression tests for the handshake-era MCP protocol gate."""

import asyncio
import json
from unittest.mock import patch

import pytest

from config import validate_mcp_oauth_configuration
from services import mcp_oauth
from services.mcp_protocol_gate import MCPProtocolGate


async def _call_gate(headers: list[tuple[bytes, bytes]], path: str = "/mcp") -> list[dict]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})

    scope = {"type": "http", "path": path, "headers": headers, "method": "POST"}
    await MCPProtocolGate(app)(scope, receive, send)
    return messages, called


@pytest.mark.asyncio
async def test_challenge_uses_configured_public_base_url():
    with patch("services.mcp_protocol_gate.MCP_PUBLIC_BASE_URL", "https://demo.example/"):
        messages, called = await _call_gate([])
    assert not called
    assert dict(messages[0]["headers"])[b"www-authenticate"] == (
        b'Bearer resource_metadata="https://demo.example/.well-known/'
        b'oauth-protected-resource"'
    )


def test_oauth_callback_configuration_is_required_and_https():
    with patch("config.MCP_OAUTH_REDIRECT_URI", ""):
        validate_mcp_oauth_configuration()
    with patch("config.MCP_OAUTH_REDIRECT_URI", " http://client.example/callback"), pytest.raises(RuntimeError):
        validate_mcp_oauth_configuration()
    with patch(
        "config.MCP_OAUTH_REDIRECT_URI", "http://client.example/callback"
    ), pytest.raises(RuntimeError):
        validate_mcp_oauth_configuration()
    with patch("config.MCP_OAUTH_REDIRECT_URI", "https://client.example/callback"):
        validate_mcp_oauth_configuration()


async def main() -> None:
    messages, called = await _call_gate([])
    assert not called
    assert messages[0]["status"] == 401
    assert dict(messages[0]["headers"])[b"www-authenticate"] == (
        b'Bearer resource_metadata="https://mcp.pointnxt.com/.well-known/'
        b'oauth-protected-resource"'
    )

    valid_headers = [(b"authorization", b"Bearer test-token")]
    with patch.object(mcp_oauth, "validate_bearer_credential", return_value=True):
        for method in (
            "server/discover",
            "resources/list",
            "tools/list",
            "prompts/list",
            "tools/call",
            "notifications/initialized",
            "initialize",
        ):
            messages, called = await _call_gate(
                valid_headers + [(b"mcp-protocol-version", b"2026-07-28")]
            )
            assert not called, method
            assert messages[0]["status"] == 400, method
            body = json.loads(messages[1]["body"])
            assert body["error"]["message"] == "Unsupported protocol version", method
            assert body["error"]["data"]["requested"] == "2026-07-28", method
            assert body["error"]["data"]["supported"] == [
                "2024-11-05",
                "2025-03-26",
                "2025-06-18",
                "2025-11-25",
            ], method

        messages, called = await _call_gate(
            valid_headers + [(b"mcp-protocol-version", b"unknown-version")]
        )
        assert not called
        assert messages[0]["status"] == 400

        messages, called = await _call_gate(
            valid_headers + [(b"mcp-protocol-version", b"2025-11-25")]
        )
        assert called
        assert messages[0]["status"] == 200

    messages, called = await _call_gate([(b"mcp-protocol-version", b"2026-07-28")], path="/oauth/token")
    assert called
    assert messages[0]["status"] == 200


if __name__ == "__main__":
    asyncio.run(main())
    print("MCP protocol gate tests passed")
