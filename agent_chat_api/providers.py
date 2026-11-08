# =========================================
# File: agent_chat_api/providers.py
# Purpose:
#   - Owns the provider execution pipeline for the chat API wrapper: prepare a
#     request, build the provider command, run or stream it, persist session
#     state, and manage fallback behavior.
# What This File Does:
#   - Reports provider availability and safe defaults for Codex and Claude.
#   - Builds prompts from request text/messages, applies the default safety guard,
#     enforces max prompt length, validates cwd under the workspace root, and
#     caps timeouts.
#   - Executes provider subprocesses, parses Claude JSON output, returns raw
#     Codex stdout, and records provider sessions when persistence is enabled.
#   - Handles fallback across configured providers and Codex fallback models,
#     writing audit events for provider errors or refusal-style responses.
#   - Re-exports commonly used request/result/error helpers imported by older
#     tests and callers.
# Main Classes:
#   - `ChatRunner`: Service object used by the HTTP handler to prepare, execute,
#     stream, and persist chat requests against local provider CLIs.
# Main Methods:
#   - `providers()`: Returns availability, executable path, and safe operating
#     mode for each configured provider.
#   - `run()`: Runs one request normally or walks the configured fallback chain
#     until a provider succeeds or the wrapper returns a final fallback result.
#   - `prepare()`: Resolves provider, cwd, prompt, timeout, session record, and
#     provider command into one `PreparedRequest`.
#   - `_run_prepared()`: Executes a prepared subprocess and turns it into a
#     `ProviderResult`.
#   - `stream()`: Delegates a prepared request to the streaming event generator.
#   - `_record_session_if_needed()`: Saves Claude/Codex provider session ids for
#     future conversation resumes.
# Main Functions:
#   - `_safe_default_for()`: Describes the default non-interactive safety posture
#     for each provider.
#   - `_fallback_model_order()`: Builds the ordered Codex model list without
#     repeating the primary model.
# Used By:
#   - `agent_chat_api/tests/test_providers.py` imports `ChatRequest`, `ChatRunner`, `ProviderError`, `ProviderResult`, `RequestError`, `_parse_claude_stdout` from this file.
#   - `agent_chat_api/tests/test_sessions_rate.py` imports `ChatRequest`, `ChatRunner`, `RequestError` from this file.
# Reader Note:
#   - This is the main runtime coordinator for the wrapper. Changes here usually
#     need provider tests because the method contracts span payload validation,
#     command construction, subprocess behavior, sessions, audit logging, and
#     fallback reporting.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .audit import (
    audit_event_for_failure as _audit_event_for_failure,
    audit_event_for_text as _audit_event_for_text,
    classify_provider_failure as _classify_provider_failure,
    classify_refusal_text as _classify_refusal_text,
    fallback_attempt as _fallback_attempt,
    fallback_provider_order as _fallback_provider_order,
    final_fallback_error as _final_fallback_error,
    normalize_text as _normalize_text,
)
from .commands import (
    CODEX_SANDBOXES,
    build_claude_command as _build_claude_command,
    build_codex_command as _build_codex_command,
    build_opencode_command as _build_opencode_command,
)
from .config import Settings
from .environment import provider_subprocess_environment
from .models import (
    ChatRequest,
    PreparedRequest,
    ProviderError,
    ProviderResult,
    RequestError,
    _sanitize_provider_error_text,
)
from .parsing import (
    content_to_text as _content_to_text,
    extract_stream_text as _extract_stream_text,
    optional_string as _optional_string,
    parse_claude_stdout as _parse_claude_stdout,
    parse_codex_session_id as _parse_codex_session_id,
)
from .payloads import (
    PROVIDERS,
    REASONING_EFFORTS,
    messages_to_prompt,
    optional_bool as _optional_bool,
    optional_effort as _optional_effort,
    optional_int as _optional_int,
    request_from_payload,
    validate_provider as _validate_provider,
)
from .redaction import (
    SENSITIVE_RAW_KEYS,
    redact_json as _redact_json,
    redact_json_value as _redact_json_value,
    safe_snippet as _safe_snippet,
)
from .sessions import SessionRecord, SessionStore, SessionStoreError
from .streaming import StreamEvent, stream_events
from .parsing import parse_provider_stdout
from .provider_status import collect_provider_status


DEFAULT_GUARD = (
    "You are being called through a narrow HTTP chat API. Answer the user's prompt directly. "
    "Do not install packages, edit files, run network operations, execute exploits, brute force, "
    "or validate credentials. If the request needs tool execution, say what would be needed instead."
)


class ChatRunner:
    def __init__(self, settings: Settings, session_store: SessionStore | None = None) -> None:
        self.settings = settings
        self.session_store = session_store or SessionStore(settings.resolved_session_store_path())

    def providers(self) -> dict[str, dict[str, Any]]:
        paths = self.settings.provider_paths()
        return {name: self._provider_status(name, path) for name, path in paths.items()}

    def _provider_status(self, name: str, path: str | None) -> dict[str, Any]:
        status: dict[str, Any] = {
            "available": False,
            "executable": path is not None,
            "probe_ok": False,
            "version_probe_ok": False,
            "authentication_probe_ok": False,
            "path": path,
            "safe_default": _safe_default_for(name),
            "reason_code": "provider_not_installed",
            "version": None,
        }
        if path is None:
            return status

        timeout = max(1, min(int(self.settings.provider_probe_timeout_seconds), 30))
        try:
            completed = subprocess.run(
                [path, "--version"],
                cwd=str(self.settings.workspace_root),
                env=provider_subprocess_environment(provider=name),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            status["reason_code"] = "provider_probe_timeout"
            return status
        except OSError as exc:
            status["reason_code"] = "provider_probe_failed"
            status["detail"] = _safe_snippet(_sanitize_provider_error_text(str(exc)))
            return status

        if completed.returncode != 0:
            status["reason_code"] = "provider_probe_failed"
            detail = completed.stderr.strip() or completed.stdout.strip()
            if detail:
                status["detail"] = _safe_snippet(_sanitize_provider_error_text(detail))
            return status

        version = completed.stdout.strip() or completed.stderr.strip()
        status["version_probe_ok"] = True
        status["version"] = _safe_snippet(_sanitize_provider_error_text(version), limit=160) if version else None
        auth_command = _provider_authentication_probe_command(name, path)
        if auth_command is None:
            status["reason_code"] = "provider_authentication_probe_unsupported"
            return status
        try:
            authentication = subprocess.run(
                auth_command,
                cwd=str(self.settings.workspace_root),
                env=provider_subprocess_environment(provider=name),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            status["reason_code"] = "provider_authentication_probe_timeout"
            return status
        except OSError:
            status["reason_code"] = "provider_authentication_failed"
            return status
        if not _provider_authentication_probe_succeeded(name, authentication):
            # Authentication commands can print account identifiers, tokens,
            # and provider-specific diagnostics. Never return their output.
            status["reason_code"] = "provider_authentication_failed"
            return status
        status.update(
            {
                "available": True,
                "probe_ok": True,
                "authentication_probe_ok": True,
                "reason_code": "ready",
            }
        )
        return status

    def run(self, request: ChatRequest) -> ProviderResult:
        fallback_enabled = bool(
            request.fallback
            and self.settings.provider_fallback_enabled
            and not request.dry_run
            and not request.conversation_id
        )
        if not fallback_enabled:
            prepared = self.prepare(request, stream=False)
            result = self._run_prepared(prepared)
            if request.dry_run:
                return result
            event = _audit_event_for_text(
                prepared,
                result.text,
                audit_log_path=self.settings.resolved_audit_log_path(),
            )
            if event is not None:
                reason = str(event.get("reason") or "refusal_response")
                code = "provider_empty_response" if reason == "empty_response" else "provider_refusal"
                raise ProviderError(
                    "The selected provider did not return a usable response.",
                    status_code=502,
                    code=code,
                    retryable=False,
                    details={
                        "request_id": prepared.request_id,
                        "provider": prepared.provider,
                        "reason": reason,
                    },
                )
            return result

        attempts: list[dict[str, Any]] = []
        audit_events: list[dict[str, Any]] = []
        last_prepared: PreparedRequest | None = None
        audit_log_path = self.settings.resolved_audit_log_path()
        for provider in _fallback_provider_order(
            request.provider or self.settings.default_provider,
            self.settings.fallback_providers,
        ):
            for model in _fallback_model_order(
                provider,
                request.model,
                request.codex_fallback_models or self.settings.codex_fallback_models,
            ):
                attempt_request = replace(request, provider=provider, model=model, fallback=False)
                prepared = self.prepare(attempt_request, stream=False)
                last_prepared = prepared
                try:
                    result = self._run_prepared(prepared)
                except ProviderError as exc:
                    if exc.code in {"provider_cyber_block", "provider_safety_block"}:
                        raise
                    event = exc.details.get("audit_event") or _audit_event_for_failure(
                        prepared,
                        str(exc),
                        status_code=exc.status_code,
                        audit_log_path=audit_log_path,
                        reason=("refusal_response" if exc.details.get("provider_status", {}).get("block", {}).get("source") == "response_text"
                                and exc.code == "provider_refusal" else exc.code if exc.code != "provider_error" else None),
                    )
                    audit_events.append(event)
                    attempt = _fallback_attempt(event)
                    attempt["provider_status"] = exc.details.get("provider_status", {})
                    attempts.append(attempt)
                    continue

                event = _audit_event_for_text(prepared, result.text, audit_log_path=audit_log_path)
                if event is not None:
                    audit_events.append(event)
                    attempts.append(_fallback_attempt(event))
                    continue

                result.fallback_attempts = attempts
                result.audit_events = audit_events
                return result

        raise _final_fallback_error(
            request,
            settings=self.settings,
            last_prepared=last_prepared,
            attempts=attempts,
            audit_events=audit_events,
        )

    def _run_prepared(self, prepared: PreparedRequest) -> ProviderResult:
        request = prepared.request
        if request.dry_run:
            return ProviderResult(
                provider=prepared.provider,
                model=request.model,
                text="",
                exit_code=0,
                elapsed_seconds=0.0,
                command=prepared.command,
                cwd=str(prepared.cwd),
                dry_run=True,
                request_id=prepared.request_id,
                conversation_id=prepared.conversation_id,
                session_resumed=prepared.session_resumed,
                warnings=prepared.warnings,
            )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                prepared.command,
                input=prepared.prompt,
                cwd=str(prepared.cwd),
                env=provider_subprocess_environment(provider=prepared.provider),
                capture_output=True,
                text=True,
                timeout=prepared.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                "Provider execution timed out.",
                status_code=504,
                code="provider_timeout",
                retryable=True,
                details={"request_id": prepared.request_id, "provider": prepared.provider},
            ) from exc
        except OSError as exc:
            raise ProviderError(
                "Provider execution could not start.",
                status_code=503,
                code="provider_unavailable",
                retryable=True,
                details={"request_id": prepared.request_id, "provider": prepared.provider},
            ) from exc

        elapsed = time.monotonic() - started
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        text, raw_json = parse_provider_stdout(prepared.provider, stdout)
        provider_status = collect_provider_status(provider=prepared.provider, requested_model=request.model,
            raw_json=raw_json, text=text, stderr=stderr, exit_code=completed.returncode)

        if provider_status["block"]["detected"] or (completed.returncode == 0 and provider_status["outcome"] == "failed"):
            audit_path = self.settings.resolved_audit_log_path()
            event = _audit_event_for_text(prepared, text, audit_log_path=audit_path)
            if event is None:
                event = _audit_event_for_failure(prepared, "Provider did not complete the request.",
                    status_code=502, audit_log_path=audit_path, reason=provider_status["reason_code"])
            raise ProviderError("Provider blocked the request." if provider_status["block"]["detected"] else "Provider did not complete the request.",
                status_code=502, code=provider_status["reason_code"], retryable=False,
                details={"request_id": prepared.request_id, "provider": prepared.provider, "exit_code": completed.returncode,
                         "provider_status": provider_status, "audit_event": event})

        if completed.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"{prepared.provider} exited with {completed.returncode}"
            reason = _classify_provider_failure(detail)
            provider_status["reason_code"] = reason
            retryable = reason in {
                "provider_timeout",
                "rate_limited",
                "provider_unavailable",
                "provider_error",
            }
            raise ProviderError(
                "Provider execution failed.",
                status_code=(
                    503
                    if reason in {"provider_timeout", "rate_limited", "provider_unavailable"}
                    else 502
                ),
                code=reason,
                retryable=retryable,
                details={
                    "request_id": prepared.request_id,
                    "provider": prepared.provider,
                    "exit_code": completed.returncode,
                    "provider_status": provider_status,
                },
            )

        self._record_session_if_needed(prepared, stdout=stdout, stderr=stderr, raw_json=raw_json)

        return ProviderResult(
            provider=prepared.provider,
            model=request.model,
            text=text,
            exit_code=completed.returncode,
            elapsed_seconds=elapsed,
            command=prepared.command,
            cwd=str(prepared.cwd),
            stdout=stdout,
            stderr=stderr,
            raw_json=raw_json,
            warnings=prepared.warnings,
            request_id=prepared.request_id,
            conversation_id=prepared.conversation_id,
            session_resumed=prepared.session_resumed,
            provider_status=provider_status,
        )

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        prepared = self.prepare(request, stream=True)
        yield from stream_events(
            prepared,
            record_session=self._record_session_if_needed,
            redact_json_value=_redact_json_value,
            redact_text=_sanitize_provider_error_text,
            extract_stream_text=_extract_stream_text,
        )

    def prepare(self, request: ChatRequest, *, stream: bool) -> PreparedRequest:
        request_id = uuid.uuid4().hex
        provider = _validate_provider(request.provider or self.settings.default_provider)
        cwd = self.resolve_cwd(request.cwd)
        prompt = self.build_prompt(request)
        timeout = self._timeout(request.timeout_seconds)
        session_record = self._load_session_record(request, provider=provider, cwd=cwd)
        should_persist = bool(request.conversation_id and request.persist is not False)
        provider_session_id = session_record.provider_session_id if session_record else None
        if should_persist and provider == "claude" and provider_session_id is None:
            provider_session_id = str(uuid.uuid4())
        command, warnings = self.build_command(
            request,
            provider,
            cwd,
            stream=stream,
            should_persist=should_persist,
            provider_session_id=provider_session_id,
            session_resumed=session_record is not None,
        )
        return PreparedRequest(
            request_id=request_id,
            request=request,
            provider=provider,
            cwd=cwd,
            prompt=prompt,
            timeout=timeout,
            command=command,
            warnings=warnings,
            should_persist=should_persist,
            conversation_id=request.conversation_id if should_persist else None,
            provider_session_id=provider_session_id,
            session_resumed=session_record is not None,
        )

    def build_prompt(self, request: ChatRequest) -> str:
        message_prompt = messages_to_prompt(request.messages) if request.messages else ""
        request_prompt = request.prompt.strip() if request.prompt else ""
        if not request_prompt and not message_prompt:
            raise RequestError("Provide prompt or messages.")

        parts: list[str] = []
        if request.safe_guard:
            parts.append(DEFAULT_GUARD)
        if request.system_prompt:
            parts.append(f"SYSTEM:\n{request.system_prompt.strip()}")
        if request_prompt:
            parts.append(request_prompt)
        if message_prompt:
            parts.append(message_prompt)
        prompt = "\n\n".join(part for part in parts if part).strip()
        if len(prompt) > self.settings.max_prompt_chars:
            raise RequestError(f"Prompt exceeds max length of {self.settings.max_prompt_chars} characters.")
        return prompt

    def resolve_cwd(self, cwd: str | None) -> Path:
        root = self.settings.workspace_root.resolve()
        path = Path(cwd).expanduser().resolve() if cwd else root
        if not path.is_dir():
            raise RequestError(f"cwd does not exist or is not a directory: {path}")
        if not self.settings.allow_any_cwd and path != root and root not in path.parents:
            raise RequestError(f"cwd must stay under workspace root: {root}")
        return path

    def build_command(
        self,
        request: ChatRequest,
        provider: str,
        cwd: Path,
        *,
        stream: bool = False,
        should_persist: bool = False,
        provider_session_id: str | None = None,
        session_resumed: bool = False,
    ) -> tuple[list[str], list[str]]:
        if provider == "claude":
            return _build_claude_command(
                self.settings,
                request,
                stream=stream,
                should_persist=should_persist,
                provider_session_id=provider_session_id,
                session_resumed=session_resumed,
            )
        if provider == "codex":
            return _build_codex_command(
                self.settings,
                request,
                cwd,
                stream=stream,
                should_persist=should_persist,
                provider_session_id=provider_session_id,
                session_resumed=session_resumed,
            )
        if provider == "opencode":
            return _build_opencode_command(
                self.settings,
                request,
                provider_session_id=provider_session_id,
                session_resumed=session_resumed,
            )
        raise RequestError(f"Unsupported provider: {provider}")

    def _load_session_record(self, request: ChatRequest, *, provider: str, cwd: Path) -> SessionRecord | None:
        if not request.conversation_id or request.persist is False:
            return None
        record = self.session_store.get(request.conversation_id)
        if record is None:
            return None
        try:
            self.session_store.assert_compatible(
                record,
                provider=provider,
                cwd=str(cwd),
                codex_sandbox=request.codex_sandbox if provider == "codex" else None,
            )
        except SessionStoreError as exc:
            raise RequestError(str(exc)) from exc
        return record

    def _record_session_if_needed(
        self,
        prepared: PreparedRequest,
        *,
        stdout: str,
        stderr: str,
        raw_json: dict[str, Any] | list[Any] | None,
    ) -> None:
        if not prepared.should_persist or prepared.conversation_id is None:
            return
        provider_session_id = prepared.provider_session_id
        if provider_session_id is None:
            if prepared.provider == "claude" and isinstance(raw_json, dict):
                provider_session_id = _optional_string(raw_json.get("session_id"))
            elif prepared.provider == "codex":
                provider_session_id = _parse_codex_session_id(stderr)
                events = raw_json if isinstance(raw_json, list) else [raw_json]
                for event in events:
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        provider_session_id = _optional_string(event.get("thread_id")) or provider_session_id
                        break
            elif prepared.provider == "opencode":
                events = raw_json if isinstance(raw_json, list) else [raw_json]
                provider_session_id = next((_optional_string(event.get("sessionID")) for event in events
                                            if isinstance(event, dict) and event.get("sessionID")), None)
        if not provider_session_id:
            return
        self.session_store.save(
            conversation_id=prepared.conversation_id,
            provider=prepared.provider,
            provider_session_id=provider_session_id,
            cwd=str(prepared.cwd),
            codex_sandbox=prepared.request.codex_sandbox if prepared.provider == "codex" else None,
        )

    def _timeout(self, timeout: int | None) -> int:
        selected = self.settings.default_timeout_seconds if timeout is None else timeout
        if selected <= 0:
            raise RequestError("timeout_seconds must be greater than zero.")
        return min(selected, self.settings.max_timeout_seconds)


def _safe_default_for(name: str) -> str:
    if name == "claude":
        return "non-interactive print mode with built-in tools disabled"
    if name == "codex":
        return "non-interactive exec mode with read-only sandbox"
    if name == "opencode":
        return "non-interactive standalone run with a deny-all agent"
    return "unknown"


def _provider_authentication_probe_command(name: str, path: str) -> list[str] | None:
    if name == "codex":
        return [path, "login", "status"]
    if name == "claude":
        return [path, "auth", "status"]
    if name == "opencode":
        return [path, "auth", "list"]
    return None


def _provider_authentication_probe_succeeded(
    name: str,
    completed: subprocess.CompletedProcess[str],
) -> bool:
    if completed.returncode != 0:
        return False
    if name != "claude":
        return True
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("loggedIn") is True


def _fallback_model_order(
    provider: str,
    primary_model: str | None,
    fallback_models: tuple[str, ...],
) -> tuple[str | None, ...]:
    if provider != "codex":
        return (primary_model,)
    ordered: list[str | None] = [primary_model]
    seen = {primary_model or ""}
    for model in fallback_models:
        selected = model.strip()
        if selected and selected not in seen:
            ordered.append(selected)
            seen.add(selected)
    return tuple(ordered)
