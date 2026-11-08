# =========================================
# File: agent_chat_api/tests/test_providers.py
# Purpose:
#   - Exercises provider payload parsing, prompt construction, command building,
#     subprocess execution, redaction, and fallback behavior for the chat runner.
# What This File Does:
#   - Verifies request payload edge cases such as provider-prefixed models,
#     reasoning effort, malformed fields, string booleans, and message flattening.
#   - Checks Claude/Codex command argv for tool mode, models, effort, sandbox
#     rules, cwd confinement, prompt guard behavior, timeout handling, and dry
#     runs.
#   - Uses fake provider executables to test successful subprocess execution,
#     nonzero exits, redacted public output, and fallback across refusals/quota
#     failures.
# Main Classes:
#   - `ProviderTests`: Covers validation, command construction, prompt building,
#     provider execution, output parsing, public serialization, and safety
#     defaults.
#   - `ProviderFallbackTests`: Covers refusal/quota detection, audit logging,
#     successful fallback, and final wrapper fallback when every provider refuses.
# Main Methods:
#   - `test_claude_command_*()` and `test_codex_*()`: Protect provider-specific
#     command-line contracts.
#   - `test_build_prompt_*()` and payload tests: Protect request normalization
#     before provider execution.
#   - Fake binary tests: Prove the runner handles real subprocess stdout/stderr
#     paths without live model calls.
#   - Fallback tests: Prove refusal/quota audit events drive provider fallback
#     and deterministic final wrapper output.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - These tests are the main safety net for `providers.py`, `commands.py`,
#     `payloads.py`, `parsing.py`, `audit.py`, and `models.py`.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT, remove_test_bin, temporary_executable
except ModuleNotFoundError:
    from support import PACKAGE_ROOT, remove_test_bin, temporary_executable

from agent_chat_api.config import Settings
from agent_chat_api.audit import classify_provider_failure
from agent_chat_api.environment import provider_subprocess_environment
from agent_chat_api.parsing import extract_stream_text, parse_provider_stdout
from agent_chat_api.payloads import request_from_payload
from agent_chat_api.provider_status import collect_provider_status
from agent_chat_api.providers import (
    _provider_authentication_probe_command,
    ChatRequest,
    ChatRunner,
    ProviderError,
    ProviderResult,
    RequestError,
    _parse_claude_stdout,
    messages_to_prompt,
    request_from_payload,
)


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PACKAGE_ROOT
        self.settings = Settings(
            workspace_root=self.root,
            codex_bin="/usr/bin/codex",
            claude_bin="/usr/bin/claude",
        )
        self.runner = ChatRunner(self.settings)

    def tearDown(self) -> None:
        remove_test_bin(self.root)

    def test_messages_to_prompt_preserves_roles(self) -> None:
        prompt = messages_to_prompt(
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hello"},
            ]
        )
        self.assertIn("SYSTEM:\nBe concise.", prompt)
        self.assertIn("USER:\nHello", prompt)

    def test_model_prefix_selects_provider(self) -> None:
        request = request_from_payload({"model": "codex:gpt-5.5", "prompt": "x", "dry_run": True}, "claude")
        self.assertEqual(request.provider, "codex")
        self.assertEqual(request.model, "gpt-5.5")

    def test_payload_accepts_reasoning_effort(self) -> None:
        request = request_from_payload(
            {"provider": "codex", "model": "gpt-5.5", "effort": "high", "prompt": "x"},
            "claude",
        )

        self.assertEqual(request.provider, "codex")
        self.assertEqual(request.model, "gpt-5.5")
        self.assertEqual(request.effort, "high")

    def test_payload_rejects_unknown_reasoning_effort(self) -> None:
        with self.assertRaises(RequestError):
            request_from_payload({"prompt": "hello", "effort": "extreme"}, "claude")

    def test_payload_rejects_non_list_messages(self) -> None:
        with self.assertRaises(RequestError):
            request_from_payload({"messages": "hello"}, "claude")

    def test_payload_rejects_bad_integer_fields(self) -> None:
        with self.assertRaises(RequestError):
            request_from_payload({"prompt": "hello", "timeout_seconds": "soon"}, "claude")

    def test_payload_parses_string_booleans_deliberately(self) -> None:
        request = request_from_payload(
            {
                "prompt": "hello",
                "dry_run": "false",
                "include_raw": "true",
                "allow_tools": "no",
                "safe_guard": "on",
                "codex_ephemeral": "1",
            },
            "claude",
        )
        self.assertFalse(request.dry_run)
        self.assertTrue(request.include_raw)
        self.assertFalse(request.allow_tools)
        self.assertTrue(request.safe_guard)
        self.assertTrue(request.codex_ephemeral)

    def test_payload_rejects_ambiguous_boolean_values(self) -> None:
        with self.assertRaises(RequestError):
            request_from_payload({"prompt": "hello", "dry_run": "sometimes"}, "claude")

    def test_http_payload_cannot_disable_guard_or_enable_provider_tools(self) -> None:
        unsafe_payloads = (
            {"prompt": "hello", "safe_guard": False},
            {"prompt": "hello", "allow_tools": True},
            {"prompt": "hello", "claude_tools": "Read,Write"},
            {"prompt": "hello", "claude_permission_mode": "default"},
            {"prompt": "hello", "codex_sandbox": "workspace-write"},
        )
        for payload in unsafe_payloads:
            with self.subTest(payload=payload), self.assertRaises(RequestError):
                request_from_payload(payload, "codex")

    def test_claude_command_disables_tools_by_default(self) -> None:
        command, warnings = self.runner.build_command(
            ChatRequest(provider="claude", prompt="hello"),
            "claude",
            self.root,
        )
        self.assertEqual(warnings, [])
        self.assertIn("-p", command)
        self.assertIn("--output-format", command)
        self.assertIn("stream-json", command)
        self.assertIn("--verbose", command)
        self.assertNotIn("--max-turns", command)
        self.assertEqual(command[command.index("--tools") + 1], "")

    def test_claude_command_accepts_model_and_effort(self) -> None:
        command, warnings = self.runner.build_command(
            ChatRequest(provider="claude", prompt="hello", model="sonnet", effort="high"),
            "claude",
            self.root,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(command[command.index("--model") + 1], "sonnet")
        self.assertEqual(command[command.index("--effort") + 1], "high")

    def test_claude_command_allows_explicit_tool_mode(self) -> None:
        command, warnings = self.runner.build_command(
            ChatRequest(provider="claude", prompt="hello", allow_tools=True, claude_tools="Read", claude_permission_mode="default"),
            "claude",
            self.root,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(command[command.index("--tools") + 1], "Read")
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")

    def test_claude_max_turns_emits_warning_without_cli_flag(self) -> None:
        command, warnings = self.runner.build_command(
            ChatRequest(provider="claude", prompt="hello", claude_max_turns=3),
            "claude",
            self.root,
        )
        self.assertNotIn("--max-turns", command)
        self.assertTrue(warnings)

    def test_codex_command_uses_read_only_sandbox(self) -> None:
        command, warnings = self.runner.build_command(
            ChatRequest(provider="codex", prompt="hello"),
            "codex",
            self.root,
        )
        self.assertIn("exec", command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertTrue(warnings)
        self.assertEqual(command[-1], "-")

    def test_codex_workspace_write_sandbox_can_be_requested(self) -> None:
        command, _warnings = self.runner.build_command(
            ChatRequest(provider="codex", prompt="hello", codex_sandbox="workspace-write", allow_tools=True),
            "codex",
            self.root,
        )
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")

    def test_codex_danger_sandbox_is_denied_by_default(self) -> None:
        with self.assertRaises(RequestError):
            self.runner.build_command(
                ChatRequest(provider="codex", prompt="hello", codex_sandbox="danger-full-access"),
                "codex",
                self.root,
            )

    def test_invalid_provider_is_rejected(self) -> None:
        with self.assertRaises(RequestError):
            self.runner.run(ChatRequest(provider="unknown", prompt="hello", dry_run=True))

    def test_cwd_is_confined_to_workspace_root(self) -> None:
        with self.assertRaises(RequestError):
            self.runner.resolve_cwd("/tmp")

    def test_child_cwd_under_workspace_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            self.assertEqual(self.runner.resolve_cwd(tmp), Path(tmp).resolve())

    def test_dry_run_does_not_invoke_provider(self) -> None:
        result = self.runner.run(ChatRequest(provider="claude", prompt="hello", dry_run=True))
        self.assertTrue(result.dry_run)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.text, "")
        self.assertIsNotNone(result.request_id)

    def test_public_dict_redacts_raw_provider_identifiers(self) -> None:
        result = ProviderResult(
            provider="claude",
            model=None,
            text="ok",
            exit_code=0,
            elapsed_seconds=1.0,
            command=["claude"],
            cwd=str(self.root),
            stdout='{"session_id":"abc","uuid":"def","result":"ok"} OPENAI_API_KEY=stdout-secret',
            stderr="session id: 019e254f-2f20-7d61-9aa5-aa10f6aa4ced",
            raw_json={
                "session_id": "abc",
                "nested": {"uuid": "def", "OPENAI_API_KEY": "json-secret"},
                "result": "ok",
            },
            request_id="req-1",
        )
        payload = result.public_dict(include_raw=True)
        self.assertEqual(payload["request_id"], "req-1")
        self.assertIn("[redacted]", payload["stdout"])
        self.assertIn("[redacted]", payload["stderr"])
        self.assertEqual(payload["raw_json"]["session_id"], "[redacted]")
        self.assertEqual(payload["raw_json"]["nested"]["uuid"], "[redacted]")
        self.assertEqual(payload["raw_json"]["nested"]["OPENAI_API_KEY"], "[redacted]")
        self.assertEqual(payload["raw_json"]["result"], "ok")
        self.assertNotIn("stdout-secret", payload["stdout"])

    def test_public_dict_excludes_raw_payload_by_default(self) -> None:
        result = ProviderResult(
            provider="claude",
            model=None,
            text="ok",
            exit_code=0,
            elapsed_seconds=1.0,
            command=["claude"],
            cwd=str(self.root),
            stdout="secret",
            stderr="secret",
            raw_json={"session_id": "abc"},
            request_id="req-1",
        )
        payload = result.public_dict()
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)
        self.assertNotIn("raw_json", payload)
        self.assertEqual(payload["request_id"], "req-1")

    def test_build_prompt_combines_guard_system_prompt_and_messages(self) -> None:
        prompt = self.runner.build_prompt(
            ChatRequest(
                prompt="Raw prompt",
                system_prompt="System context",
                messages=[{"role": "user", "content": "Message prompt"}],
            )
        )
        self.assertIn("Do not install packages", prompt)
        self.assertIn("SYSTEM:\nSystem context", prompt)
        self.assertIn("Raw prompt", prompt)
        self.assertIn("USER:\nMessage prompt", prompt)

    def test_build_prompt_can_disable_guard(self) -> None:
        prompt = self.runner.build_prompt(ChatRequest(prompt="Only this", safe_guard=False))
        self.assertEqual(prompt, "Only this")

    def test_build_prompt_rejects_empty_or_oversized_prompt(self) -> None:
        with self.assertRaises(RequestError):
            self.runner.build_prompt(ChatRequest())
        runner = ChatRunner(Settings(workspace_root=self.root, max_prompt_chars=3))
        with self.assertRaises(RequestError):
            runner.build_prompt(ChatRequest(prompt="abcd"))

    def test_timeout_is_capped_and_validated(self) -> None:
        runner = ChatRunner(Settings(workspace_root=self.root, default_timeout_seconds=10, max_timeout_seconds=20))
        self.assertEqual(runner._timeout(None), 10)
        self.assertEqual(runner._timeout(99), 20)
        with self.assertRaises(RequestError):
            runner._timeout(0)

    def test_parse_claude_stdout_handles_json_and_plain_text(self) -> None:
        text, raw = _parse_claude_stdout('{"result":"hello"}')
        self.assertEqual(text, "hello")
        self.assertIsInstance(raw, dict)
        text, raw = _parse_claude_stdout('{"content":[{"type":"text","text":"one"},{"type":"text","text":"two"}]}')
        self.assertEqual(text, "one\ntwo")
        self.assertIsInstance(raw, dict)
        text, raw = _parse_claude_stdout("plain")
        self.assertEqual(text, "plain")
        self.assertIsNone(raw)

    def test_run_uses_fake_claude_binary_without_live_model_call(self) -> None:
        fake_claude = temporary_executable(
            self.root,
            "fake-claude",
            """
stdin = sys.stdin.read()
print('{"result":"fake claude saw ' + str(len(stdin)) + ' chars"}')
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, claude_bin=str(fake_claude)))
        result = runner.run(ChatRequest(provider="claude", prompt="hello", safe_guard=False))
        self.assertEqual(result.text, "fake claude saw 5 chars")
        self.assertEqual(result.exit_code, 0)

    def test_run_uses_fake_codex_binary_without_live_model_call(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex",
            """
stdin = sys.stdin.read()
print('fake codex saw ' + str(len(stdin)) + ' chars')
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, codex_bin=str(fake_codex)))
        result = runner.run(ChatRequest(provider="codex", prompt="hello", safe_guard=False))
        self.assertEqual(result.text, "fake codex saw 5 chars")
        self.assertEqual(result.exit_code, 0)

    def test_provider_subprocess_receives_allowlisted_environment_only(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-environment-boundary",
            """
import os
print('|'.join([
    os.environ.get('PATH', ''),
    os.environ.get('HOST_APPLICATION_DATABASE_URL', 'missing'),
    os.environ.get('HOST_APPLICATION_REVIEW_SENTINEL', 'missing'),
]))
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, codex_bin=str(fake_codex)))
        with patch.dict(
            os.environ,
            {
                "HOST_APPLICATION_DATABASE_URL": "postgresql://secret",
                "HOST_APPLICATION_REVIEW_SENTINEL": "review-secret",
            },
            clear=False,
        ):
            result = runner.run(ChatRequest(provider="codex", prompt="hello", safe_guard=False))

        path_value, database_value, sentinel_value = result.text.split("|", 2)
        self.assertTrue(path_value)
        self.assertEqual(database_value, "missing")
        self.assertEqual(sentinel_value, "missing")

    def test_explicit_provider_refusal_is_a_typed_nonretryable_failure(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-explicit-refusal",
            """
print("I cannot help with that request.")
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, codex_bin=str(fake_codex)))

        with self.assertRaises(ProviderError) as raised:
            runner.run(ChatRequest(provider="codex", prompt="hello", safe_guard=False))

        self.assertEqual(raised.exception.code, "provider_refusal")
        self.assertFalse(raised.exception.retryable)

    def test_provider_authentication_failures_are_classified_nonretryable(self) -> None:
        self.assertEqual(
            classify_provider_failure("HTTP 401 Unauthorized: login required"),
            "provider_auth_failed",
        )

    def test_codex_command_accepts_model_and_effort(self) -> None:
        command, warnings = self.runner.build_command(
            ChatRequest(provider="codex", prompt="hello", model="gpt-5.5", effort="high"),
            "codex",
            self.root,
        )

        self.assertTrue(any("read-only sandbox" in warning for warning in warnings))
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.5")
        self.assertIn("-c", command)
        self.assertIn('model_reasoning_effort="high"', command)

    def test_provider_nonzero_exit_becomes_provider_error(self) -> None:
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-fail",
            """
sys.stderr.write('provider failed')
raise SystemExit(7)
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, claude_bin=str(fake_claude)))
        with self.assertRaises(ProviderError) as raised:
            runner.run(ChatRequest(provider="claude", prompt="hello", safe_guard=False))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(str(raised.exception), "Provider execution failed.")

    def test_provider_error_redacts_secret_bearing_stderr_and_details(self) -> None:
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-secret-error",
            """
sys.stderr.write('request failed Authorization: Bearer provider-secret {"api_key":"raw-secret"} OPENAI_API_KEY=env-secret')
raise SystemExit(7)
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, claude_bin=str(fake_claude)))

        with self.assertRaises(ProviderError) as raised:
            runner.run(ChatRequest(provider="claude", prompt="hello", safe_guard=False))

        payload = raised.exception.public_payload()
        self.assertNotIn("provider-secret", str(raised.exception))
        self.assertNotIn("raw-secret", str(raised.exception))
        self.assertNotIn("env-secret", str(raised.exception))
        self.assertEqual(str(raised.exception), "Provider execution failed.")
        self.assertNotIn("provider-secret", str(payload))
        self.assertNotIn("raw-secret", str(payload))
        self.assertNotIn("env-secret", str(payload))

    def test_provider_readiness_requires_successful_executable_probe(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-broken-version",
            """
if '--version' in sys.argv:
    sys.stderr.write('broken provider')
    raise SystemExit(9)
print('should not be used')
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, codex_bin=str(fake_codex)))

        status = runner.providers()["codex"]

        self.assertFalse(status["available"])
        self.assertTrue(status["executable"])
        self.assertFalse(status["probe_ok"])
        self.assertEqual(status["reason_code"], "provider_probe_failed")

    def test_provider_readiness_requires_successful_authentication_probe(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-auth-failure",
            """
if '--version' in sys.argv:
    print('codex 1.0')
    raise SystemExit(0)
if sys.argv[1:] == ['login', 'status']:
    sys.stderr.write('not logged in OPENAI_API_KEY=must-not-leak')
    raise SystemExit(7)
raise SystemExit(0)
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, codex_bin=str(fake_codex)))

        status = runner.providers()["codex"]

        self.assertFalse(status["available"])
        self.assertTrue(status["executable"])
        self.assertFalse(status["probe_ok"])
        self.assertFalse(status["authentication_probe_ok"])
        self.assertEqual(status["reason_code"], "provider_authentication_failed")
        self.assertNotIn("must-not-leak", str(status))

    def test_provider_readiness_accepts_version_and_authentication_probes(self) -> None:
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-auth-ready",
            """
if '--version' in sys.argv:
    print('claude 1.0')
    raise SystemExit(0)
if sys.argv[1:] == ['auth', 'status']:
    print('{"loggedIn":true,"email":"must-not-be-returned@example.test"}')
    raise SystemExit(0)
raise SystemExit(0)
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, claude_bin=str(fake_claude)))

        status = runner.providers()["claude"]

        self.assertTrue(status["available"])
        self.assertTrue(status["probe_ok"])
        self.assertTrue(status["authentication_probe_ok"])
        self.assertEqual(status["reason_code"], "ready")
        self.assertNotIn("must-not-be-returned", str(status))

    def test_claude_auth_status_must_explicitly_report_logged_in(self) -> None:
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-auth-false",
            """
if '--version' in sys.argv:
    print('claude 1.0')
    raise SystemExit(0)
if sys.argv[1:] == ['auth', 'status']:
    print('{"loggedIn":false,"email":"must-not-be-returned@example.test"}')
    raise SystemExit(0)
raise SystemExit(0)
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, claude_bin=str(fake_claude)))

        status = runner.providers()["claude"]

        self.assertFalse(status["available"])
        self.assertFalse(status["authentication_probe_ok"])
        self.assertEqual(status["reason_code"], "provider_authentication_failed")
        self.assertNotIn("must-not-be-returned", str(status))

    def test_streaming_provider_error_redacts_environment_style_secrets(self) -> None:
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-stream-secret-error",
            """
sys.stderr.write('OPENAI_API_KEY=stream-secret')
raise SystemExit(8)
""",
        )
        runner = ChatRunner(Settings(workspace_root=self.root, claude_bin=str(fake_claude)))

        events = list(runner.stream(ChatRequest(provider="claude", prompt="hello", safe_guard=False, stream=True)))

        errors = [event.data["error"] for event in events if event.event == "error"]
        self.assertEqual(len(errors), 1)
        self.assertNotIn("stream-secret", errors[0])
        self.assertIn("[redacted]", errors[0])


class ProviderFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PACKAGE_ROOT

    def tearDown(self) -> None:
        remove_test_bin(self.root)

    def test_fallback_uses_claude_after_codex_refusal_and_audits(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-refusal",
            """
print("Sorry, I cannot do that.")
""",
        )
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-fallback",
            """
print('{"result":"fallback ok"}')
""",
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "provider-audit.jsonl"
            runner = ChatRunner(
                Settings(
                    workspace_root=self.root,
                    codex_bin=str(fake_codex),
                    claude_bin=str(fake_claude),
                    audit_log_path=audit_path,
                )
            )
            result = runner.run(ChatRequest(provider="codex", prompt="hello", safe_guard=False, fallback=True))

            self.assertEqual(result.provider, "claude")
            self.assertEqual(result.text, "fallback ok")
            self.assertEqual(len(result.fallback_attempts), 1)
            self.assertEqual(result.fallback_attempts[0]["provider"], "codex")
            self.assertEqual(result.fallback_attempts[0]["reason"], "refusal_response")
            self.assertEqual(result.fallback_attempts[0]["provider_status"]["block"]["source"], "response_text")
            audit_lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(audit_lines[0]["type"], "provider_refusal")
            self.assertEqual(audit_lines[0]["reason"], "refusal_response")
            self.assertNotIn("hello", audit_lines[0]["snippet"])

    def test_fallback_uses_claude_after_codex_quota_error(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-quota",
            """
sys.stderr.write("usage limit reached")
raise SystemExit(1)
""",
        )
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-after-quota",
            """
print('{"result":"quota fallback ok"}')
""",
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = ChatRunner(
                Settings(
                    workspace_root=self.root,
                    codex_bin=str(fake_codex),
                    claude_bin=str(fake_claude),
                    audit_log_path=Path(tmp) / "provider-audit.jsonl",
                )
            )
            result = runner.run(ChatRequest(provider="codex", prompt="hello", safe_guard=False, fallback=True))

        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.text, "quota fallback ok")
        self.assertEqual(result.fallback_attempts[0]["reason"], "quota_exhausted")
        self.assertEqual(result.fallback_attempts[0]["provider_status"]["reason_code"], "quota_exhausted")

    def test_exhausted_fallbacks_raise_typed_provider_failure(self) -> None:
        fake_codex = temporary_executable(
            self.root,
            "fake-codex-refuses",
            """
print("I can't help with that.")
""",
        )
        fake_claude = temporary_executable(
            self.root,
            "fake-claude-refuses",
            """
print('{"result":"I cannot assist with that request."}')
""",
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = ChatRunner(
                Settings(
                    workspace_root=self.root,
                    codex_bin=str(fake_codex),
                    claude_bin=str(fake_claude),
                    audit_log_path=Path(tmp) / "provider-audit.jsonl",
                )
            )
            with self.assertRaises(ProviderError) as raised:
                runner.run(ChatRequest(provider="codex", prompt="hello", safe_guard=False, fallback=True))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "provider_fallback_exhausted")
        self.assertTrue(all("provider_status" in item for item in raised.exception.details["attempts"]))
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(
            [item["provider"] for item in raised.exception.details["attempts"]],
            ["codex", "claude"],
        )


if __name__ == "__main__":
    unittest.main()


class OpenCodeProviderTests(unittest.TestCase):
    # Events are verbatim from `opencode run --format json` on OpenCode 2.0.24.
    TEXT = ('{"type":"text","timestamp":1,"sessionID":"ses_abc","part":{"id":"prt_1","sessionID":"ses_abc",'
            '"messageID":"msg_2","type":"text","text":"PONG","time":{"start":1,"end":2}}}\n')
    QUOTA = ('{"type":"error","timestamp":1,"sessionID":"ses_abc","error":{"type":"provider.quota",'
             '"message":"Free usage limit reached.","status":429}}\n')

    def _settings(self) -> Settings:
        return Settings(workspace_root=PACKAGE_ROOT, opencode_bin="/opt/opencode")

    def test_read_only_requests_use_a_private_server_and_the_deny_all_agent(self) -> None:
        prepared = ChatRunner(self._settings()).prepare(
            ChatRequest(provider="opencode", prompt="hi", model="openrouter/z-ai/glm-5.3"), stream=False)
        self.assertEqual(prepared.command, ["/opt/opencode", "run", "--standalone", "--format", "json",
                                            "--agent", "omc-advisor", "--model", "openrouter/z-ai/glm-5.3"])
        self.assertNotIn("hi", " ".join(prepared.command), "prompts must not reach argv")

    def test_tools_are_only_available_on_request(self) -> None:
        prepared = ChatRunner(self._settings()).prepare(
            ChatRequest(provider="opencode", prompt="hi", allow_tools=True), stream=False)
        self.assertNotIn("--agent", prepared.command)

    def test_request_environment_carries_the_agent_and_blocks_gpu_autostart(self) -> None:
        environment = provider_subprocess_environment({"PATH": "/bin", "SECRET_THING": "x"}, provider="opencode")
        agent = json.loads(environment["OPENCODE_CONFIG_CONTENT"])["agents"]["omc-advisor"]
        self.assertEqual(agent["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}])
        self.assertEqual(environment["OPENCODE_NO_AUTOLLAMA"], "1")
        self.assertNotIn("SECRET_THING", environment)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", provider_subprocess_environment({"PATH": "/bin"}))

    def test_model_prefix_selects_opencode(self) -> None:
        request = request_from_payload({"model": "opencode:ollama/qwen3:30b", "prompt": "hi"}, "codex")
        self.assertEqual((request.provider, request.model), ("opencode", "ollama/qwen3:30b"))

    def test_text_events_become_the_answer(self) -> None:
        text, events = parse_provider_stdout("opencode", self.TEXT)
        self.assertEqual(text, "PONG")
        self.assertEqual(extract_stream_text(json.loads(self.TEXT)), "PONG")

    def test_only_the_last_message_is_the_answer(self) -> None:
        def line(message: str, value: str) -> str:
            return json.dumps({"type": "text", "sessionID": "s", "part": {"messageID": message, "text": value}}) + "\n"
        text, _ = parse_provider_stdout("opencode", line("m1", "Let me check.") + line("m2", "Fi") + line("m2", "nal"))
        self.assertEqual(text, "Final")

    def test_success_and_failure_map_to_provider_status(self) -> None:
        text, raw = parse_provider_stdout("opencode", self.TEXT)
        status = collect_provider_status(provider="opencode", requested_model="a/b", raw_json=raw, text=text, stderr="", exit_code=0)
        self.assertEqual(status["outcome"], "completed")
        self.assertIsNone(status["actual_model"], "OpenCode does not report one, so it stays unknown")
        text, raw = parse_provider_stdout("opencode", self.QUOTA)
        status = collect_provider_status(provider="opencode", requested_model="a/b", raw_json=raw, text=text, stderr="", exit_code=1)
        self.assertEqual((status["outcome"], status["reason_code"]), ("failed", "provider_error"))
        self.assertEqual(status["signals"][0]["code"], "provider.quota")
        self.assertFalse(status["block"]["detected"], "quota is not a policy block")

    def test_empty_output_is_not_success(self) -> None:
        status = collect_provider_status(provider="opencode", requested_model=None, raw_json=None, text="", stderr="", exit_code=0)
        self.assertEqual(status["reason_code"], "provider_empty_response")

    def test_conversations_resume_the_opencode_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = ChatRunner(Settings(workspace_root=PACKAGE_ROOT, opencode_bin="/opt/opencode",
                                         session_store_path=Path(directory) / "sessions.json"))
            request = ChatRequest(provider="opencode", prompt="hi", conversation_id="conversation-1")
            prepared = runner.prepare(request, stream=False)
            self.assertNotIn("--session", prepared.command)
            _, raw = parse_provider_stdout("opencode", self.TEXT)
            runner._record_session_if_needed(prepared, stdout=self.TEXT, stderr="", raw_json=raw)
            resumed = runner.prepare(request, stream=False)
            self.assertEqual(resumed.command[resumed.command.index("--session") + 1], "ses_abc")

    def test_authentication_probe_uses_auth_list(self) -> None:
        self.assertEqual(_provider_authentication_probe_command("opencode", "/opt/opencode"), ["/opt/opencode", "auth", "list"])
