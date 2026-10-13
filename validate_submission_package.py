"""Validate the local PointNXT remote-MCP submission draft.

Usage:
    .\\.venv\\Scripts\\python.exe validate_submission_package.py

This script is read-only. It validates the allowlisted manifests, optionally
retrieves the official Agent Plugins schemas, checks known metadata, reports
pending team/portal requirements, and refuses to report submission readiness
while required items are missing. It never creates a ZIP or reads secret file
contents.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PACKAGE_ALLOWLIST = ("plugin.json", "mcp.json")
SCHEMA_URLS = {
    "plugin.json": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
    "mcp.json": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
}
EXPECTED_ENDPOINT = "https://mcp.pointnxt.com/mcp"
EXPECTED_DISPLAY_NAME = "PointNXT MCP"
EXPECTED_PUBLISHER = "PointNXT"

FORBIDDEN_NAME_PATTERNS = (
    re.compile(r"^\.env(?:\..*)?$", re.IGNORECASE),
    re.compile(r"(?:session|runtime|state).*\.(?:sqlite3?|db)$", re.IGNORECASE),
    re.compile(r"(?:credential|secret|token|password|private.?key|\.pem$|\.key$)", re.IGNORECASE),
)
FORBIDDEN_DIRECTORY_NAMES = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "tmp",
    "temp",
    "output",
    "runtime",
    "sessions",
}


def load_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        errors.append(f"missing manifest: {path.name}")
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"invalid JSON in {path.name}: {error}")
        return None
    if not isinstance(value, dict):
        errors.append(f"manifest must contain a JSON object: {path.name}")
        return None
    return value


def fetch_schema(url: str) -> tuple[dict[str, Any] | None, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": "PointNXT-local-validator/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as error:
        return None, str(error)


def validate_schema(filename: str, document: dict[str, Any]) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["schema validation not performed: jsonschema is not installed"]

    schema, error = fetch_schema(SCHEMA_URLS[filename])
    if schema is None:
        return [f"schema validation not performed for {filename}: {error}"]

    return [
        f"{filename} {list(issue.absolute_path) or ['/']}: {issue.message}"
        for issue in sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda issue: list(issue.absolute_path),
        )
    ]


def forbidden_paths() -> list[str]:
    findings: list[str] = []
    # Only inspect names at the repository root. Forbidden directories are
    # rejected as package inputs without walking or opening their contents.
    for path in ROOT.iterdir():
        if path.name in FORBIDDEN_DIRECTORY_NAMES or any(pattern.search(path.name) for pattern in FORBIDDEN_NAME_PATTERNS):
            findings.append(path.name)
    return sorted(set(findings))


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    print("PointNXT local submission-package validation")
    print(f"Allowlisted package files: {', '.join(PACKAGE_ALLOWLIST)}")

    for filename in PACKAGE_ALLOWLIST:
        path = ROOT / filename
        if path.exists() and not path.is_file():
            errors.append(f"allowlisted manifest is not a regular file: {filename}")

    plugin = load_json(ROOT / "plugin.json", errors)
    mcp = load_json(ROOT / "mcp.json", errors)

    if plugin is not None:
        schema_findings = validate_schema("plugin.json", plugin)
        for finding in schema_findings:
            (warnings if finding.startswith("schema validation not performed") else errors).append(finding)
    if mcp is not None:
        schema_findings = validate_schema("mcp.json", mcp)
        for finding in schema_findings:
            (warnings if finding.startswith("schema validation not performed") else errors).append(finding)

    if plugin is not None:
        interface = plugin.get("extensions", {}).get("com.openai", {}).get("interface", {})
        if interface.get("displayName") != EXPECTED_DISPLAY_NAME:
            errors.append("plugin.json interface.displayName does not match PointNXT MCP")
        if plugin.get("author", {}).get("name") != EXPECTED_PUBLISHER:
            errors.append("plugin.json author.name does not match PointNXT")
        if interface.get("websiteURL") != "https://pointnxt.com":
            errors.append("plugin.json interface.websiteURL is missing or inconsistent")
        if not interface.get("shortDescription") or not interface.get("longDescription"):
            errors.append("plugin.json is missing shortDescription or longDescription")

    if mcp is not None:
        server = mcp.get("mcpServers", {}).get("pointnxt", {})
        if server.get("type") != "streamable-http":
            errors.append("mcp.json pointnxt server must use streamable-http")
        if server.get("url") != EXPECTED_ENDPOINT:
            errors.append("mcp.json endpoint does not match the intended production endpoint")

    pending = {
        "privacy policy URL",
        "terms of service URL",
        "support URL",
        "approved logo/icon",
        "demo recording URL",
        "dedicated reviewer account and approved sample data",
        "OAuth client registration and live verification",
        "portal domain-verification challenge",
        "current production MCP scan",
        "publisher verification and policy attestations",
    }
    for item in sorted(pending):
        warnings.append(f"pending requirement: {item}")

    forbidden = forbidden_paths()
    print(f"Forbidden-path scan: {len(forbidden)} excluded root paths found; none are package candidates.")
    if forbidden:
        print("  Local paths are excluded by policy and were not opened: " + ", ".join(forbidden))

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")

    if errors:
        print("RESULT: NOT READY — validation errors require correction.")
        return 1
    if any("schema validation not performed" in warning for warning in warnings):
        print("RESULT: NOT READY — schema validation was not performed and pending requirements remain.")
        return 2
    print("RESULT: NOT READY — pending team-owned or portal-only requirements remain.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
