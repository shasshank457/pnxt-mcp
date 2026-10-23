import pytest
from services.tool_errors import StructuredToolError
from unittest.mock import AsyncMock, patch

from tools.returns import confirm_return_request, create_support_ticket, find_pending_returns, get_return_details

@pytest.mark.asyncio
async def test_get_return_details_reuses_order_return_lookup():
    with patch("tools.returns.get_order_returns", new=AsyncMock(return_value={"found": True, "returns": []})) as lookup:
        result = await get_return_details(order_reference="#42")
    lookup.assert_awaited_once_with("#42")
    assert result["found"]

@pytest.mark.asyncio
async def test_find_pending_returns_uses_real_status():
    with patch("tools.returns.search_returns", new=AsyncMock(return_value={"returns": []})) as search:
        await find_pending_returns(limit=5, page=2)
    search.assert_awaited_once_with(limit=5, page=2, return_status="RETURN_REQUESTED", customer_id=None, warehouse_id=None)

@pytest.mark.asyncio
async def test_confirm_return_request_never_silently_writes():
    with patch("tools.returns.get_order_returns", new=AsyncMock(return_value={"found": True, "returns": [{"status": "RETURN_REQUESTED"}]})):
        preview = await confirm_return_request("42")
        with pytest.raises(StructuredToolError) as error:
            await confirm_return_request("42", confirm=True)
    assert preview["status"] == "preview"
    assert error.value.code == "UNSUPPORTED_OPERATION"
    assert error.value.details["write_performed"] is False

@pytest.mark.asyncio
async def test_support_ticket_reports_missing_backend_without_fake_record():
    with pytest.raises(StructuredToolError) as error:
        await create_support_ticket("Problem", "Something went wrong")
    assert error.value.code == "UNSUPPORTED_OPERATION"
    assert error.value.details["write_performed"] is False
