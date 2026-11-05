# =========================================
# File: agent_chat_api/redaction.py
# Purpose:
#   - Removes sensitive identifiers and credentials before provider output,
#     raw JSON, stderr, or audit snippets are returned or persisted.
# What This File Does:
#   - Recursively redacts sensitive JSON keys such as API keys, authorization,
#     tokens, session ids, and UUIDs while preserving non-sensitive structure.
#   - Scrubs bearer tokens and key/value secrets from arbitrary text.
#   - Produces compact one-line snippets suitable for audit logs and fallback
#     reports.
# Main Functions:
#   - `redact_json()`: Deep-copies raw provider JSON before scrubbing so callers
#     do not lose their original object unexpectedly.
#   - `redact_json_value()`: Walks nested dict/list structures and replaces
#     sensitive values with `[redacted]`.
#   - `redact_text()`: Applies regex redaction to freeform provider text.
#   - `safe_snippet()`: Redacts, flattens whitespace, and truncates long text for
#     safe audit display.
# Used By:
#   - `ProviderResult.public_dict()` uses it before exposing raw stdout/stderr or
#     raw provider JSON.
#   - `agent_chat_api.audit` and `agent_chat_api.streaming` use it before logging
#     or emitting provider details.
# Reader Note:
#   - Be conservative when adding patterns here. False negatives can leak
#     secrets, but overly broad replacements can make operator debugging
#     impossible.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


SENSITIVE_RAW_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "session_id",
    "uuid",
}


def redact_json(value: dict[str, Any] | list[Any] | None) -> dict[str, Any] | list[Any] | None:
    if value is None:
        return None
    return redact_json_value(deepcopy(value))


def redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_RAW_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_json_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    return value


def redact_text(value: str) -> str:
    redacted = value
    redacted = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/\-=]+", r"\1[redacted]", redacted)
    redacted = re.sub(
        r'("?(?:api_key|apikey|authorization|access_token|refresh_token|session_id|uuid)"?\s*[:=]\s*")([^"]+)(")',
        r"\1[redacted]\3",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r"((?:session id|session_id|uuid)\s*[:=]\s*)[A-Za-z0-9._~+/\-=:-]+",
        r"\1[redacted]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def safe_snippet(value: str, limit: int = 220) -> str:
    redacted = redact_text(value).replace("\r", " ").replace("\n", " ").strip()
    redacted = re.sub(r"\s+", " ", redacted)
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 3].rstrip() + "..."
