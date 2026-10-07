"""Safe resolution and validation of OAuth Client ID Metadata Documents."""

import asyncio
import ipaddress
import json
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx
from config import MCP_CIMD_ALLOWED_HOSTS


class ClientMetadataError(ValueError):
    """The client metadata URL or document is invalid or unsafe."""


_MAX_DOCUMENT_BYTES = 64 * 1024
_MAX_REDIRECTS = 2
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 128
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


def validate_metadata_url(value: str) -> str:
    """Validate the stable HTTPS URL used as a CIMD client_id."""
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ClientMetadataError("invalid client metadata URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or not parsed.path
        or parsed.path == "/"
    ):
        raise ClientMetadataError("invalid client metadata URL")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise ClientMetadataError("invalid client metadata URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ClientMetadataError("invalid client metadata URL") from exc
    return value


def _public_addresses(hostname: str) -> list[str]:
    if hostname.lower() not in MCP_CIMD_ALLOWED_HOSTS:
        raise ClientMetadataError("metadata host is not allowlisted")
    try:
        results = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ClientMetadataError("metadata host cannot be resolved") from exc
    addresses = {item[4][0] for item in results}
    if not addresses:
        raise ClientMetadataError("metadata host cannot be resolved")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ClientMetadataError("metadata host resolved to an invalid address") from exc
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ClientMetadataError("metadata host resolves to a restricted address")
    return sorted(addresses)


def _validate_redirect_uri(value: object) -> str:
    if not isinstance(value, str):
        raise ClientMetadataError("invalid redirect URI")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ClientMetadataError("invalid redirect URI") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ClientMetadataError("invalid redirect URI")
    return value


def validate_metadata(client_id: str, document: object) -> dict:
    if not isinstance(document, dict):
        raise ClientMetadataError("client metadata must be a JSON object")
    if document.get("client_id") != client_id:
        raise ClientMetadataError("client metadata client_id mismatch")
    if not isinstance(document.get("client_name"), str) or not document["client_name"].strip():
        raise ClientMetadataError("client metadata client_name is required")
    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ClientMetadataError("client metadata redirect_uris is required")
    validated = [_validate_redirect_uri(value) for value in redirect_uris]
    if len(set(validated)) != len(validated):
        raise ClientMetadataError("client metadata contains duplicate redirect URIs")
    grant_types = document.get("grant_types", ["authorization_code"])
    response_types = document.get("response_types", ["code"])
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise ClientMetadataError("client does not support authorization_code")
    if not isinstance(response_types, list) or "code" not in response_types:
        raise ClientMetadataError("client does not support code response")
    auth_method = document.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        raise ClientMetadataError("only public PKCE clients are supported")
    return {**document, "redirect_uris": validated}


async def _fetch_document(url: str) -> dict:
    current = validate_metadata_url(url)
    timeout = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            _public_addresses(urlsplit(current).hostname or "")
            async with client.stream("GET", current, headers={"Accept": "application/json"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ClientMetadataError("metadata redirect has no location")
                    current = validate_metadata_url(urljoin(current, location))
                    continue
                if response.status_code != 200:
                    raise ClientMetadataError("metadata document unavailable")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise ClientMetadataError("invalid metadata content length") from exc
                    if declared_length > _MAX_DOCUMENT_BYTES:
                        raise ClientMetadataError("metadata document is too large")
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_DOCUMENT_BYTES:
                        raise ClientMetadataError("metadata document is too large")
                    chunks.append(chunk)
                body = b"".join(chunks)
            try:
                document = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClientMetadataError("metadata document is not valid JSON") from exc
            return validate_metadata(url, document)
    raise ClientMetadataError("too many metadata redirects")


async def resolve_client_metadata(client_id: str) -> dict:
    validate_metadata_url(client_id)
    now = time.monotonic()
    async with _cache_lock:
        cached = _cache.get(client_id)
        if cached and cached[0] > now:
            return cached[1]
    document = await _fetch_document(client_id)
    async with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            oldest = min(_cache, key=lambda key: _cache[key][0])
            _cache.pop(oldest, None)
        _cache[client_id] = (time.monotonic() + _CACHE_TTL_SECONDS, document)
    return document


def clear_metadata_cache_for_tests() -> None:
    _cache.clear()
