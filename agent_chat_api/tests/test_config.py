# =========================================
# File: agent_chat_api/tests/test_config.py
# Purpose:
#   - Verifies that `agent_chat_api.config` reads defaults and `CHAT_API_*`
#     environment overrides into the expected `Settings` values.
# What This File Does:
#   - Confirms the default workspace root is the package directory rather than
#     the whole repo.
#   - Exercises host/port/auth/workspace/provider/timeout/binary/session/audit
#     and rate-limit environment options.
#   - Checks boolean and integer parsing fallbacks and provider path discovery.
# Main Classes:
#   - `ConfigTests`: Focused unittest case covering default settings,
#     environment parsing, and provider binary path reporting.
# Main Methods:
#   - `test_settings_from_env_reads_runtime_options()`: Covers the broad
#     environment contract in one explicit fixture.
#   - `test_env_bool_and_int_fallbacks()`: Protects permissive fallback behavior
#     for malformed or missing values.
#   - Provider path tests distinguish missing absolute binaries from existing
#     absolute paths.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - Update these tests when adding a `Settings` field or changing any
#     `CHAT_API_*` environment variable name/default.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT
except ModuleNotFoundError:
    from support import PACKAGE_ROOT

from agent_chat_api.config import Settings, _env_bool, _env_int, default_workspace_root


class ConfigTests(unittest.TestCase):
    def test_default_workspace_root_is_subfolder(self) -> None:
        self.assertEqual(default_workspace_root(), PACKAGE_ROOT)

    def test_settings_from_env_uses_subfolder_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.workspace_root, PACKAGE_ROOT)
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8787)
        self.assertEqual(settings.default_provider, "codex")
        self.assertEqual(settings.fallback_providers, ("codex", "claude"))
        self.assertTrue(settings.provider_fallback_enabled)
        self.assertFalse(settings.allow_unauthenticated_posts)
        self.assertEqual(settings.provider_probe_timeout_seconds, 3)
        self.assertEqual(settings.max_request_body_bytes, 1_048_576)
        self.assertEqual(settings.max_concurrent_requests, 8)
        self.assertEqual(settings.socket_io_timeout_seconds, 15)

    def test_settings_from_env_reads_runtime_options(self) -> None:
        env = {
            "CHAT_API_HOST": "0.0.0.0",
            "CHAT_API_PORT": "9999",
            "CHAT_API_KEY": "secret",
            "CHAT_API_ALLOW_UNAUTHENTICATED_POSTS": "true",
            "CHAT_API_WORKSPACE_ROOT": str(PACKAGE_ROOT),
            "CHAT_API_ALLOW_ANY_CWD": "true",
            "CHAT_API_DEFAULT_PROVIDER": "codex",
            "CHAT_API_FALLBACK_PROVIDERS": "codex,claude",
            "CHAT_API_PROVIDER_FALLBACK_ENABLED": "false",
            "CHAT_API_TIMEOUT_SECONDS": "12",
            "CHAT_API_MAX_TIMEOUT_SECONDS": "34",
            "CHAT_API_MAX_PROMPT_CHARS": "56",
            "CHAT_API_MAX_REQUEST_BODY_BYTES": "4096",
            "CHAT_API_MAX_CONCURRENT_REQUESTS": "4",
            "CHAT_API_SOCKET_IO_TIMEOUT_SECONDS": "9",
            "CHAT_API_CODEX_BIN": "/tmp/codex-test",
            "CHAT_API_CLAUDE_BIN": "/tmp/claude-test",
            "CHAT_API_ALLOW_DANGEROUS_SANDBOXES": "1",
            "CHAT_API_SESSION_STORE": str(PACKAGE_ROOT / "runtime" / "custom-sessions.json"),
            "CHAT_API_AUDIT_LOG": str(PACKAGE_ROOT / "runtime" / "custom-audit.jsonl"),
            "CHAT_API_RATE_LIMIT_ENABLED": "false",
            "CHAT_API_RATE_LIMIT_WINDOW_SECONDS": "7",
            "CHAT_API_RATE_LIMIT_REQUESTS": "8",
            "CHAT_API_PROVIDER_PROBE_TIMEOUT_SECONDS": "2",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9999)
        self.assertEqual(settings.api_key, "secret")
        self.assertTrue(settings.allow_unauthenticated_posts)
        self.assertEqual(settings.workspace_root, PACKAGE_ROOT)
        self.assertTrue(settings.allow_any_cwd)
        self.assertEqual(settings.default_provider, "codex")
        self.assertEqual(settings.fallback_providers, ("codex", "claude"))
        self.assertFalse(settings.provider_fallback_enabled)
        self.assertEqual(settings.default_timeout_seconds, 12)
        self.assertEqual(settings.max_timeout_seconds, 34)
        self.assertEqual(settings.max_prompt_chars, 56)
        self.assertEqual(settings.max_request_body_bytes, 4096)
        self.assertEqual(settings.max_concurrent_requests, 4)
        self.assertEqual(settings.socket_io_timeout_seconds, 9)
        self.assertEqual(settings.codex_bin, "/tmp/codex-test")
        self.assertEqual(settings.claude_bin, "/tmp/claude-test")
        self.assertTrue(settings.allow_dangerous_sandboxes)
        self.assertEqual(settings.resolved_session_store_path(), PACKAGE_ROOT / "runtime" / "custom-sessions.json")
        self.assertEqual(settings.resolved_audit_log_path(), PACKAGE_ROOT / "runtime" / "custom-audit.jsonl")
        self.assertFalse(settings.rate_limit_enabled)
        self.assertEqual(settings.rate_limit_window_seconds, 7)
        self.assertEqual(settings.rate_limit_requests, 8)
        self.assertEqual(settings.provider_probe_timeout_seconds, 2)

    def test_env_bool_and_int_fallbacks(self) -> None:
        with patch.dict(os.environ, {"FLAG": "yes", "BROKEN": "nope"}, clear=True):
            self.assertTrue(_env_bool("FLAG"))
            self.assertFalse(_env_bool("MISSING"))
            self.assertEqual(_env_int("BROKEN", 44), 44)
            self.assertEqual(_env_int("MISSING", 55), 55)

    def test_http_resource_limits_fall_back_when_env_values_are_out_of_range(self) -> None:
        env = {
            "CHAT_API_MAX_REQUEST_BODY_BYTES": "16777217",
            "CHAT_API_MAX_CONCURRENT_REQUESTS": "0",
            "CHAT_API_SOCKET_IO_TIMEOUT_SECONDS": "301",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.max_request_body_bytes, 1_048_576)
        self.assertEqual(settings.max_concurrent_requests, 8)
        self.assertEqual(settings.socket_io_timeout_seconds, 15)

    def test_provider_paths_report_missing_absolute_binary(self) -> None:
        settings = Settings(workspace_root=PACKAGE_ROOT, codex_bin="/missing/codex", claude_bin="/missing/claude")
        self.assertEqual(settings.provider_paths(), {"codex": None, "claude": None})

    def test_provider_paths_reject_existing_non_executable_files(self) -> None:
        settings = Settings(workspace_root=PACKAGE_ROOT, codex_bin=str(Path(__file__)), claude_bin=str(Path(__file__)))
        self.assertEqual(settings.provider_paths(), {"codex": None, "claude": None})


if __name__ == "__main__":
    unittest.main()
