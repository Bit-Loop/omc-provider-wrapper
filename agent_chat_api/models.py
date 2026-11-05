# =========================================
# File: agent_chat_api/models.py
# Purpose:
#   - Defines the shared request, prepared-request, provider-result, and error
#     types passed between the API server, command builders, provider runners,
#     and response serialization.
# What This File Does:
#   - Keeps incoming chat request options in one dataclass so payload parsing,
#     command construction, session persistence, and provider execution agree on
#     the same field names.
#   - Stores subprocess/provider outcomes in `ProviderResult` and exposes a
#     redacted public representation suitable for API responses.
#   - Separates user/request validation failures from provider execution
#     failures with distinct exception types.
# Main Classes:
#   - `RequestError`: Raised when the caller submits invalid payload or option
#     data.
#   - `ProviderError`: Runtime provider failure with an HTTP-style status code.
#   - `ChatRequest`: Normalized user request, including provider choice,
#     prompt/messages, model, cwd, timeout, streaming, persistence, tools, and
#     Codex/Claude-specific options.
#   - `ProviderResult`: Provider execution result plus warnings, fallback
#     attempts, audit events, and a `public_dict()` response formatter.
#   - `PreparedRequest`: Fully resolved request state after validation, cwd
#     checks, command construction, session decisions, and request id selection.
# Main Methods:
#   - `ProviderResult.public_dict()`: Builds the JSON-safe API response and only
#     includes redacted stdout/stderr/raw JSON when `include_raw` is explicitly
#     requested.
# Used By:
#   - Most `agent_chat_api` modules import these types for validation,
#     execution, fallback auditing, and response generation.
#   - Tests use the dataclasses directly to construct focused request/provider
#     scenarios without running real provider binaries.
# Reader Note:
#   - Treat this file as the API contract for the wrapper. Field changes here
#     need matching updates in payload parsing, command construction, provider
#     tests, and server response expectations.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redaction import redact_json, redact_text


_PROVIDER_SECRET_ASSIGNMENT = re.compile(
    r"(\b[A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|token)\b\s*[:=]\s*)"
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,;}]+)',
    flags=re.IGNORECASE,
)
_PROVIDER_SECRET_ARGUMENT = re.compile(
    r"(--(?:api-key|access-token|refresh-token|authorization|password|secret|token)\s+)[^\s]+",
    flags=re.IGNORECASE,
)


def _sanitize_provider_error_text(value: str) -> str:
    redacted = redact_text(value)
    redacted = _PROVIDER_SECRET_ASSIGNMENT.sub(r"\1[redacted]", redacted)
    return _PROVIDER_SECRET_ARGUMENT.sub(r"\1[redacted]", redacted)


def _is_sensitive_provider_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    exact = {
        "api_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "authorization",
        "password",
        "secret",
        "token",
        "session_id",
        "uuid",
    }
    suffixes = tuple(f"_{item}" for item in exact if item not in {"token", "uuid"})
    return normalized in exact or normalized.endswith(suffixes)


def _redact_detail_strings(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _is_sensitive_provider_key(key) else _redact_detail_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_detail_strings(item) for item in value]
    if isinstance(value, str):
        return _sanitize_provider_error_text(value)
    return value


class RequestError(ValueError):
    pass


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        code: str = "provider_error",
        retryable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(_sanitize_provider_error_text(message))
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        redacted_details = redact_json(details or {})
        self.details = _redact_detail_strings(redacted_details or {})

    def public_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass
class ChatRequest:
    provider: str | None = None
    prompt: str | None = None
    messages: list[dict[str, Any]] | None = None
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    timeout_seconds: int | None = None
    system_prompt: str | None = None
    include_raw: bool = False
    dry_run: bool = False
    stream: bool = False
    fallback: bool = False
    conversation_id: str | None = None
    persist: bool | None = None
    allow_tools: bool = False
    claude_tools: str = ""
    claude_permission_mode: str = "dontAsk"
    claude_max_turns: int = 1
    codex_sandbox: str = "read-only"
    codex_ephemeral: bool = True
    codex_fallback_models: tuple[str, ...] = ()
    safe_guard: bool = True


@dataclass
class ProviderResult:
    provider: str
    model: str | None
    text: str
    exit_code: int
    elapsed_seconds: float
    command: list[str]
    cwd: str
    stdout: str = ""
    stderr: str = ""
    raw_json: dict[str, Any] | list[Any] | None = None
    dry_run: bool = False
    request_id: str | None = None
    conversation_id: str | None = None
    session_resumed: bool = False
    warnings: list[str] = field(default_factory=list)
    fallback_attempts: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    final_fallback: bool = False
    provider_status: dict[str, Any] = field(default_factory=dict)

    def public_dict(self, include_raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "text": self.text,
            "exit_code": self.exit_code,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "cwd": self.cwd,
            "dry_run": self.dry_run,
            "conversation_id": self.conversation_id,
            "session_resumed": self.session_resumed,
            "command": shlex.join(self.command),
            "warnings": self.warnings,
            "fallback": {
                "used": bool(self.fallback_attempts),
                "attempts": self.fallback_attempts,
                "final": self.final_fallback,
            },
            "audit_events": self.audit_events,
            "provider_status": _redact_detail_strings(redact_json(self.provider_status)),
        }
        if include_raw:
            data["stdout"] = _sanitize_provider_error_text(self.stdout)
            data["stderr"] = _sanitize_provider_error_text(self.stderr)
            data["raw_json"] = _redact_detail_strings(redact_json(self.raw_json))
        return data


@dataclass(frozen=True)
class PreparedRequest:
    request_id: str
    request: ChatRequest
    provider: str
    cwd: Path
    prompt: str
    timeout: int
    command: list[str]
    warnings: list[str]
    should_persist: bool
    conversation_id: str | None
    provider_session_id: str | None
    session_resumed: bool
