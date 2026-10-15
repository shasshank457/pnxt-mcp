"""Shared customer contact normalization and order matching helpers."""

import re
from typing import Any


def normalize_email(value: str | None) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def normalize_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _order_contact_values(order: dict[str, Any]) -> tuple[set[str], set[str]]:
    customer = order.get("customer") or {}
    address = order.get("billingAddress") or order.get("shippingAddressSnapshot") or {}
    emails = {
        normalized
        for normalized in (
            normalize_email(customer.get("email")),
            normalize_email(address.get("email")),
        )
        if normalized
    }
    phones = {
        normalized
        for normalized in (
            normalize_phone(customer.get("phone")),
            normalize_phone(address.get("phone")),
        )
        if normalized
    }
    return emails, phones


def order_matches_contact(
    order: dict[str, Any], email: str | None = None, phone: str | None = None
) -> bool:
    emails, phones = _order_contact_values(order)
    normalized_email = normalize_email(email)
    normalized_phone = normalize_phone(phone)
    return bool(
        (normalized_email and normalized_email in emails)
        or (normalized_phone and normalized_phone in phones)
    )
