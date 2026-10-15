from unittest.mock import patch

import pytest

from tools.auth import logout


def _registered_tools():
    from server import mcp

    return mcp._tool_manager._tools


def test_logout_is_not_model_exposed():
    assert "logout_pointnxt" not in _registered_tools()


@pytest.mark.asyncio
async def test_logout_function_remains_available_directly():
    with patch("tools.auth.get_session", return_value=None), patch(
        "tools.auth.invalidate_session"
    ) as invalidate:
        result = await logout()

    invalidate.assert_called_once_with()
    assert result == {
        "authenticated": False,
        "message": "You have been signed out of PointNXT.",
    }
