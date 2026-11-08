# =========================================
# File: agent_chat_api/config.py
# Purpose:
#   - Centralizes all runtime configuration for the standalone chat API wrapper.
# What This File Does:
#   - Reads bind settings, required-by-default POST bearer auth, workspace
#     limits, provider defaults, timeouts, executable probe controls, binary
#     paths, audit/session paths, and rate-limit settings from `CHAT_API_*`
#     environment variables.
#   - Provides small environment parsing helpers that fall back safely when
#     values are missing or malformed.
#   - Resolves provider binary paths using `shutil.which()` while still allowing
#     explicit paths.
# Main Classes:
#   - `Settings`: Frozen dataclass used by the CLI, server, provider runner,
#     session store, audit logger, and rate limiter.
# Main Methods:
#   - `Settings.from_env()`: Creates a complete settings object from process
#     environment with defaults for local development.
#   - `resolved_session_store_path()` / `resolved_audit_log_path()`: Return the
#     effective runtime file locations even when optional overrides are absent.
#   - `provider_paths()`: Reports only configured Codex, Claude and OpenCode binaries that
#     resolve to executable files; `ChatRunner` performs the bounded version
#     version and authentication-status probes used for readiness.
# Main Functions:
#   - `default_workspace_root()`: Uses the package directory as the default
#     workspace when no environment override is provided.
#   - `_env_bool()`, `_env_int()`, `_env_int_in_range()`,
#     `_env_provider_order()`, `_env_model_order()`: Parse environment values
#     into strongly shaped settings fields.
# Used By:
#   - `agent_chat_api/app.py` imports `Settings` from this file.
#   - `agent_chat_api/tests/test_config.py` imports `Settings`, `_env_bool`, `_env_int`, `default_workspace_root` from this file.
#   - `agent_chat_api/tests/test_providers.py` imports `Settings` from this file.
#   - `agent_chat_api/tests/test_server.py` imports `Settings` from this file.
#   - `agent_chat_api/tests/test_sessions_rate.py` imports `Settings` from this file.
# Reader Note:
#   - When adding a setting, update `from_env()`, any server/provider consumer,
#     and the config tests together so defaults and environment behavior stay
#     explicit.
#   - `CHAT_API_ALLOW_UNAUTHENTICATED_POSTS=true` is an explicit development or
#     test escape hatch. Production/default operation requires `CHAT_API_KEY`.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def default_workspace_root() -> Path:
    return Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_int_in_range(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _env_int(name, default)
    if minimum <= value <= maximum:
        return value
    return default


def _env_provider_order(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    providers = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    return providers or default


def _env_model_order(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip()) or default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    api_key: str | None = None
    allow_unauthenticated_posts: bool = False
    workspace_root: Path = field(default_factory=default_workspace_root)
    allow_any_cwd: bool = False
    default_provider: str = "codex"
    fallback_providers: tuple[str, ...] = ("codex", "claude")
    codex_fallback_models: tuple[str, ...] = ()
    provider_fallback_enabled: bool = True
    default_timeout_seconds: int = 300
    max_timeout_seconds: int = 900
    max_prompt_chars: int = 200_000
    max_request_body_bytes: int = 1_048_576
    max_concurrent_requests: int = 8
    socket_io_timeout_seconds: int = 15
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    opencode_bin: str = "opencode"
    provider_probe_timeout_seconds: int = 3
    allow_dangerous_sandboxes: bool = False
    session_store_path: Path | None = None
    audit_log_path: Path | None = None
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_requests: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        workspace_root = Path(os.getenv("CHAT_API_WORKSPACE_ROOT", default_workspace_root())).expanduser().resolve()
        session_path = os.getenv("CHAT_API_SESSION_STORE")
        audit_path = os.getenv("CHAT_API_AUDIT_LOG")
        return cls(
            host=os.getenv("CHAT_API_HOST", "127.0.0.1"),
            port=_env_int("CHAT_API_PORT", 8787),
            api_key=(os.getenv("CHAT_API_KEY") or "").strip() or None,
            allow_unauthenticated_posts=_env_bool("CHAT_API_ALLOW_UNAUTHENTICATED_POSTS", False),
            workspace_root=workspace_root,
            allow_any_cwd=_env_bool("CHAT_API_ALLOW_ANY_CWD", False),
            default_provider=os.getenv("CHAT_API_DEFAULT_PROVIDER", "codex").strip().lower() or "codex",
            fallback_providers=_env_provider_order("CHAT_API_FALLBACK_PROVIDERS", ("codex", "claude")),
            codex_fallback_models=_env_model_order("CHAT_API_CODEX_FALLBACK_MODELS"),
            provider_fallback_enabled=_env_bool("CHAT_API_PROVIDER_FALLBACK_ENABLED", True),
            default_timeout_seconds=_env_int("CHAT_API_TIMEOUT_SECONDS", 300),
            max_timeout_seconds=_env_int("CHAT_API_MAX_TIMEOUT_SECONDS", 900),
            max_prompt_chars=_env_int("CHAT_API_MAX_PROMPT_CHARS", 200_000),
            max_request_body_bytes=_env_int_in_range(
                "CHAT_API_MAX_REQUEST_BODY_BYTES",
                1_048_576,
                minimum=1,
                maximum=16_777_216,
            ),
            max_concurrent_requests=_env_int_in_range(
                "CHAT_API_MAX_CONCURRENT_REQUESTS",
                8,
                minimum=1,
                maximum=128,
            ),
            socket_io_timeout_seconds=_env_int_in_range(
                "CHAT_API_SOCKET_IO_TIMEOUT_SECONDS",
                15,
                minimum=1,
                maximum=300,
            ),
            codex_bin=os.getenv("CHAT_API_CODEX_BIN") or shutil.which("codex") or "codex",
            claude_bin=os.getenv("CHAT_API_CLAUDE_BIN") or shutil.which("claude") or "claude",
            opencode_bin=os.getenv("CHAT_API_OPENCODE_BIN") or shutil.which("opencode") or "opencode",
            provider_probe_timeout_seconds=_env_int("CHAT_API_PROVIDER_PROBE_TIMEOUT_SECONDS", 3),
            allow_dangerous_sandboxes=_env_bool("CHAT_API_ALLOW_DANGEROUS_SANDBOXES", False),
            session_store_path=Path(session_path).expanduser().resolve() if session_path else workspace_root / "runtime" / "sessions.json",
            audit_log_path=Path(audit_path).expanduser().resolve() if audit_path else workspace_root / "runtime" / "provider-audit.jsonl",
            rate_limit_enabled=_env_bool("CHAT_API_RATE_LIMIT_ENABLED", True),
            rate_limit_window_seconds=_env_int("CHAT_API_RATE_LIMIT_WINDOW_SECONDS", 60),
            rate_limit_requests=_env_int("CHAT_API_RATE_LIMIT_REQUESTS", 30),
        )

    def resolved_session_store_path(self) -> Path:
        return self.session_store_path or self.workspace_root / "runtime" / "sessions.json"

    def resolved_audit_log_path(self) -> Path:
        return self.audit_log_path or self.workspace_root / "runtime" / "provider-audit.jsonl"

    def provider_paths(self) -> dict[str, str | None]:
        def executable_path(command: str) -> str | None:
            located = shutil.which(command)
            if located:
                return located
            candidate = Path(command).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            return None

        return {
            "codex": executable_path(self.codex_bin),
            "claude": executable_path(self.claude_bin),
            "opencode": executable_path(self.opencode_bin),
        }
