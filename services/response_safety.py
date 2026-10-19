"""Remove backend-only fields and mask personal data at the MCP boundary."""

import re
from typing import Any

_REMOVED_KEYS = frozenset(
    {
        "config",
        "raw",
        "tenantid",
        "tenant_id",
        "customer_ip_address",
        "payment_url",
        "order_key",
        "cart_hash",
        "address_1",
        "address_2",
        "shippingaddresssnapshot",
    }
)
_HTML_TAG = re.compile(r"<[^>]*>")


def _mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def _mask_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) < 3:
        return "***"
    return f"***{digits[-2:]}"


def sanitize_public_response(value: Any, key: str = "") -> Any:
    """Recursively remove internal fields and mask contact identifiers."""
    normalized_key = key.casefold()
    if isinstance(value, dict):
        result = {}
        for name, child in value.items():
            child_key = str(name).casefold()
            if child_key in _REMOVED_KEYS:
                continue
            if child_key in {"email", "customeremail"} and isinstance(child, str):
                result[name] = _mask_email(child)
            elif child_key in {
                "phone",
                "customerphone",
                "mobile",
                "mobilephone",
            } and isinstance(child, str):
                result[name] = _mask_phone(child)
            elif child_key == "channelcustomerid" and isinstance(child, str):
                result[name] = "***"
            elif child_key in {"description", "shortdescription"} and isinstance(child, str):
                result[name] = _HTML_TAG.sub("", child)
            else:
                result[name] = sanitize_public_response(child, child_key)
        return result
    if isinstance(value, list):
        return [sanitize_public_response(item, normalized_key) for item in value]
    return value


__all__ = ["sanitize_public_response"]
