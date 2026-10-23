from unittest.mock import AsyncMock, patch

import pytest

from services.tool_errors import StructuredToolError
from tools.operations import (
    add_order_note,
    get_courier_rates,
    retry_delivery,
    update_support_ticket,
)


@pytest.mark.asyncio
async def test_rates_fail_closed_without_prices():
    with pytest.raises(StructuredToolError, match="UNSUPPORTED_OPERATION") as error:
        await get_courier_rates(origin_postal_code="110001", destination_postal_code="400001")
    assert error.value.code == "UNSUPPORTED_OPERATION"
    assert error.value.details["write_performed"] is False


@pytest.mark.asyncio
async def test_retry_validates_order_and_requires_confirmation():
    with patch("tools.operations.get_order", new=AsyncMock(return_value={"found": True, "order": {"shipments": []}})):
        preview = await retry_delivery("100")
        with pytest.raises(StructuredToolError) as error:
            await retry_delivery("100", confirm=True)
    assert preview["status"] == "preview"
    assert preview["confirmation_required"] is True
    assert error.value.code == "UNSUPPORTED_OPERATION"


@pytest.mark.asyncio
async def test_note_missing_order_is_rejected():
    with patch("tools.operations.get_order", new=AsyncMock(return_value={"found": False})), pytest.raises(ValueError, match="Order was not found"):
        await add_order_note("missing", "check shipment")


@pytest.mark.asyncio
async def test_ticket_update_requires_a_field_and_fails_closed():
    with pytest.raises(ValueError, match="at least one"):
        await update_support_ticket("ticket-1")
    with pytest.raises(StructuredToolError) as error:
        await update_support_ticket("ticket-1", status="CLOSED")
    assert error.value.code == "UNSUPPORTED_OPERATION"


@pytest.mark.asyncio
async def test_customer_note_requires_supported_visibility():
    with pytest.raises(ValueError, match="visibility"):
        await add_order_note("100", "hello", visibility="public")
