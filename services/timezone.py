"""Shared timezone and local-calendar-date handling for business tools."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import DEFAULT_TIMEZONE


def resolve_timezone(name: str | None = None) -> tuple[str, ZoneInfo]:
    """Return the requested IANA timezone, or the configured default."""
    value = name or DEFAULT_TIMEZONE
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a valid IANA timezone name")
    value = value.strip()
    try:
        return value, ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"timezone must be a valid IANA timezone name: {value}") from error


def _parse_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from error
    if value != parsed.isoformat():
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    return parsed


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def local_date_period(
    start_date: str | None,
    end_date: str | None,
    timezone_name: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, str | None]:
    """Resolve local date filters and their inclusive UTC boundaries."""
    name, zone = resolve_timezone(timezone_name)
    if start_date is None and end_date is None:
        return {"timezone": name, "utc_start": None, "utc_end": None}
    start = _parse_date(start_date, "start_date") if start_date else None
    end = _parse_date(end_date, "end_date") if end_date else None
    if start and end and end < start:
        raise ValueError("end_date cannot be before start_date")
    utc_start = (
        utc_iso(datetime.combine(start, time.min, tzinfo=zone)) if start else None
    )
    utc_end = (
        utc_iso(datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone) - timedelta(milliseconds=1))
        if end
        else None
    )
    return {
        "timezone": name,
        "utc_start": utc_start,
        "utc_end": utc_end,
    }


def local_today(timezone_name: str | None = None, *, now: datetime | None = None) -> str:
    _, zone = resolve_timezone(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(zone).date().isoformat()


def local_timestamp(value: str | None, timezone_name: str | None = None) -> str | None:
    """Convert a backend UTC timestamp to the requested local timezone."""
    if not value or "T" not in value:
        return value
    _, zone = resolve_timezone(timezone_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone).isoformat()
