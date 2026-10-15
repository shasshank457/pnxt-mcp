from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.timezone import local_date_period, local_timestamp, local_today
from tools.business import get_business_summary
from tools.orders import get_orders


def test_ist_today_and_utc_day_boundaries():
    now = datetime(2026, 10, 14, 20, 30, tzinfo=timezone.utc)
    assert local_today("Asia/Kolkata", now=now) == "2026-10-15"
    assert local_date_period("2026-10-15", "2026-10-15", "Asia/Kolkata") == {
        "timezone": "Asia/Kolkata",
        "utc_start": "2026-10-14T18:30:00.000Z",
        "utc_end": "2026-10-15T18:29:59.999Z",
    }


def test_utc_timezone_preserves_utc_day_boundaries():
    period = local_date_period("2026-10-15", "2026-10-15", "UTC")
    assert period["utc_start"] == "2026-10-15T00:00:00.000Z"
    assert period["utc_end"] == "2026-10-15T23:59:59.999Z"


def test_invalid_timezone_is_rejected():
    with pytest.raises(ValueError, match="valid IANA timezone"):
        local_date_period("2026-10-15", "2026-10-15", "Not/AZone")


@pytest.mark.asyncio
async def test_order_filter_sends_local_day_as_utc_boundaries():
    api = MagicMock()
    api.async_get = AsyncMock(return_value={"data": {"items": []}})
    with patch("tools.orders.get_api", return_value=api):
        result = await get_orders(
            start_date="2026-10-15",
            end_date="2026-10-15",
            timezone="Asia/Kolkata",
        )

    params = api.async_get.await_args.kwargs["params"]
    assert params["dateFrom"] == "2026-10-14T18:30:00.000Z"
    assert params["dateTo"] == "2026-10-15T18:29:59.999Z"
    assert result["timezone"] == "Asia/Kolkata"


def test_order_date_local_keeps_original_utc_value():
    utc_value = "2026-10-14T20:00:00Z"
    assert local_timestamp(utc_value, "Asia/Kolkata") == "2026-10-15T01:30:00+05:30"
    assert local_timestamp(utc_value, "UTC") == "2026-10-14T20:00:00+00:00"


@pytest.mark.asyncio
async def test_business_summary_defaults_today_to_configured_timezone():
    with patch("tools.business.local_today", return_value="2026-10-15"), \
         patch("tools.business.get_orders", new=AsyncMock(return_value={"data": {"items": []}})), \
         patch("tools.business.get_order_stats", new=AsyncMock(return_value={"data": {}})), \
         patch("tools.business.get_product_summary", new=AsyncMock(return_value={"data": {}})):
        result = await get_business_summary()

    assert result["period"]["date"] == "2026-10-15"
    assert result["period"]["timezone"] == "Asia/Kolkata"
