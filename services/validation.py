"""Shared validation for read-only MCP filters."""
import re
from datetime import datetime
from typing import Any

MAX_FILTER_LENGTH = 200
MAX_PAGE_SIZE = 100

def page_limit(limit: int, page: int, maximum: int = MAX_PAGE_SIZE) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be an integer between 1 and {maximum}")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")

def optional_text(value: str | None, name: str, *, required: bool = False) -> None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value.strip()) > MAX_FILTER_LENGTH:
        raise ValueError(f"{name} must be at most {MAX_FILTER_LENGTH} characters")

def optional_id(value: str | None, name: str) -> None:
    optional_text(value, name)
    if value is not None and not re.fullmatch(r"[A-Za-z0-9_:#./-]+", value.strip()):
        raise ValueError(f"{name} contains invalid characters")

def optional_date(value: str | None, name: str) -> None:
    optional_text(value, name)
    if value is not None:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(f"{name} must use YYYY-MM-DD format") from error
