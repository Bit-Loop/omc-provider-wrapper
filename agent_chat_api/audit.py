# =========================================
# File: agent_chat_api/audit.py
# Purpose:
#   - Records provider failure/refusal events and turns exhausted fallback chains
#     into a typed failure instead of fabricating model output.
# What This File Does:
#   - Normalizes provider fallback order, skipping invalid provider names rather
#     than letting bad configuration break the whole request.
#   - Classifies quota, rate limit, timeout, missing binary, and refusal-style
#     failures into compact audit reasons.
#   - Writes JSONL audit events with redacted snippets and owner-only file
#     permissions.
#   - Builds the typed provider error used when all real providers fail or
#     refuse.
# Main Functions:
#   - `fallback_provider_order()`: Produces a deduplicated provider order from
#     the primary provider plus configured fallbacks.
#   - `audit_event_for_failure()` and `audit_event_for_text()`: Convert provider
#     failures or refusal-like text into persisted audit events.
#   - `write_audit_event()`: Appends compact JSON to the audit log while trying
#     to keep both directory and file permissions private.
#   - `final_fallback_error()`: Returns a typed failure with the collected
#     attempts after every provider path is exhausted.
#   - `classify_provider_failure()` / `classify_refusal_text()`: Map raw stderr
#     or model text into stable reason strings consumed by fallback reporting.
# Used By:
#   - `agent_chat_api.providers` uses this module during provider execution and
#     fallback handling.
#   - `agent_chat_api.server` surfaces the resulting audit/fallback metadata in
#     API responses.
# Reader Note:
#   - Treat this file as the safety record for provider failures: changes here
#     affect what operators can audit after Codex or Claude refuses, times out,
#     or hits usage limits.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ChatRequest, PreparedRequest, ProviderError, RequestError
from .payloads import validate_provider
from .redaction import safe_snippet


def fallback_provider_order(primary: str, fallback_providers: tuple[str, ...]) -> list[str]:
    order: list[str] = []
    for provider in (primary, *fallback_providers):
        try:
            normalized = validate_provider(provider)
        except RequestError:
            continue
        if normalized not in order:
            order.append(normalized)
    return order


def audit_event_for_failure(
    prepared: PreparedRequest,
    message: str,
    *,
    status_code: int,
    audit_log_path: Path,
    reason: str | None = None,
) -> dict[str, Any]:
    return audit_event(
        "provider_failure",
        prepared=prepared,
        reason=reason or classify_provider_failure(message),
        status_code=status_code,
        snippet=message,
        audit_log_path=audit_log_path,
    )


def audit_event_for_text(prepared: PreparedRequest, text: str, *, audit_log_path: Path) -> dict[str, Any] | None:
    reason = classify_refusal_text(text)
    if reason is None:
        return None
    return audit_event(
        "provider_refusal",
        prepared=prepared,
        reason=reason,
        status_code=200,
        snippet=text,
        audit_log_path=audit_log_path,
    )


def audit_event(
    event_type: str,
    *,
    prepared: PreparedRequest,
    reason: str,
    status_code: int,
    snippet: str,
    audit_log_path: Path,
) -> dict[str, Any]:
    event = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": event_type,
        "provider": prepared.provider,
        "reason": reason,
        "request_id": prepared.request_id,
        "status_code": status_code,
        "snippet": safe_snippet(snippet),
    }
    write_audit_event(audit_log_path, event)
    return event


def write_audit_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def fallback_attempt(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": event.get("provider"),
        "reason": event.get("reason"),
        "status_code": event.get("status_code"),
        "request_id": event.get("request_id"),
        "snippet": event.get("snippet"),
    }


def final_fallback_error(
    request: ChatRequest,
    *,
    settings: Settings,
    last_prepared: PreparedRequest | None,
    attempts: list[dict[str, Any]],
    audit_events: list[dict[str, Any]],
) -> ProviderError:
    del request, settings
    retryable_reasons = {"provider_timeout", "rate_limited", "provider_unavailable", "provider_error"}
    retryable = any(str(item.get("reason") or "") in retryable_reasons for item in attempts)
    return ProviderError(
        "All configured provider fallbacks were exhausted.",
        status_code=503,
        code="provider_fallback_exhausted",
        retryable=retryable,
        details={
            "request_id": last_prepared.request_id if last_prepared else uuid.uuid4().hex,
            "attempts": attempts,
            "audit_events": audit_events,
        },
    )


def classify_provider_failure(message: str) -> str:
    normalized = normalize_text(message)
    if any(
        token in normalized
        for token in (
            "401",
            "403",
            "unauthorized",
            "authentication failed",
            "login required",
            "not logged in",
            "invalid api key",
            "invalid token",
            "expired token",
        )
    ):
        return "provider_auth_failed"
    if any(token in normalized for token in ("usage limit", "usage cap", "no usage", "quota", "insufficient_quota", "credit balance")):
        return "quota_exhausted"
    if any(token in normalized for token in ("too many requests", "rate limit", "429")):
        return "rate_limited"
    if "timed out" in normalized or "timeout" in normalized:
        return "provider_timeout"
    if "failed to start" in normalized or "no such file" in normalized or "not found" in normalized:
        return "provider_unavailable"
    refusal = classify_refusal_text(message)
    if refusal is not None:
        return refusal
    return "provider_error"


def classify_refusal_text(text: str) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return "empty_response"
    refusal_patterns = (
        r"\bsorry[, ]+i (?:can'?t|cannot|am unable to|won'?t|will not)\b",
        r"\bi (?:can'?t|cannot) (?:assist|help|comply|do that|provide)\b",
        r"\bi am unable to (?:assist|help|comply|provide)\b",
        r"\bi'?m unable to (?:assist|help|comply|provide)\b",
        r"\bi (?:won'?t|will not) (?:assist|help|comply|provide)\b",
        r"\bcan'?t help with that\b",
        r"\bcannot help with that\b",
        r"\bagainst (?:policy|safety policy)\b",
        r"\bpolicy (?:does not allow|prohibits)\b",
    )
    for pattern in refusal_patterns:
        if re.search(pattern, normalized):
            return "refusal_response"
    return None


def normalize_text(value: str) -> str:
    return value.replace("’", "'").replace("`", "'").strip().lower()
