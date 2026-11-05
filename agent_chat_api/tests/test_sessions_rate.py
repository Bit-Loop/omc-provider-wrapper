# =========================================
# File: agent_chat_api/tests/test_sessions_rate.py
# Purpose:
#   - Verifies conversation persistence, provider session resume behavior, Codex
#     model fallback, and in-memory rate limiting.
# What This File Does:
#   - Uses fake Claude and Codex executables to prove provider session ids are
#     captured, stored, and used to build future resume commands.
#   - Protects safety rules around provider mismatch, `persist=False`, and Codex
#     sandbox compatibility for existing conversations.
#   - Checks Codex fallback models retry after a primary model failure and that
#     rate-limit buckets block after fixed-window capacity.
# Main Classes:
#   - `SessionAndRateTests`: Combined regression case for session storage,
#     provider resume command generation, model fallback, and limiter buckets.
# Main Methods:
#   - Claude/Codex persistence tests cover session id storage and command resume
#     flags.
#   - Conversation safety tests cover mismatch rejection and ephemeral reuse.
#   - Rate-limit tests cover fixed-window capacity and auth-token hashing.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - Keep this file close to changes in `sessions.py`, `providers.py`, and
#     `rate_limit.py`; it protects cross-module behavior rather than a single
#     helper.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from pathlib import Path

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT, remove_test_bin, temporary_executable
except ModuleNotFoundError:
    from support import PACKAGE_ROOT, remove_test_bin, temporary_executable

from agent_chat_api.config import Settings
from agent_chat_api.providers import ChatRequest, ChatRunner, RequestError
from agent_chat_api.rate_limit import InMemoryRateLimiter, bucket_key_for_request
from agent_chat_api.sessions import SessionStore


class SessionAndRateTests(unittest.TestCase):
    def tearDown(self) -> None:
        remove_test_bin(PACKAGE_ROOT)

    def test_claude_persistent_conversation_creates_and_resumes(self) -> None:
        fake_claude = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-session",
            """
print('{"result":"ok","session_id":"ignored-session"}')
""",
        )
        with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as tmp:
            settings = Settings(workspace_root=PACKAGE_ROOT, claude_bin=str(fake_claude), session_store_path=Path(tmp) / "sessions.json")
            runner = ChatRunner(settings)
            first = runner.run(ChatRequest(provider="claude", prompt="hello", conversation_id="conv-1", safe_guard=False))
            self.assertEqual(first.conversation_id, "conv-1")
            self.assertFalse(first.session_resumed)
            record = SessionStore(settings.resolved_session_store_path()).get("conv-1")
            self.assertIsNotNone(record)

            second = runner.prepare(ChatRequest(provider="claude", prompt="again", conversation_id="conv-1"), stream=False)
            self.assertTrue(second.session_resumed)
            self.assertIn("--resume", second.command)
            self.assertEqual(second.command[second.command.index("--resume") + 1], record.provider_session_id)

    def test_codex_persistent_conversation_parses_session_id_and_resumes(self) -> None:
        fake_codex = temporary_executable(
            PACKAGE_ROOT,
            "fake-codex-session",
            """
sys.stderr.write('session id: codex-session-1\\n')
print('ok')
""",
        )
        with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as tmp:
            settings = Settings(workspace_root=PACKAGE_ROOT, codex_bin=str(fake_codex), session_store_path=Path(tmp) / "sessions.json")
            runner = ChatRunner(settings)
            first = runner.run(ChatRequest(provider="codex", prompt="hello", conversation_id="conv-1", safe_guard=False))
            self.assertEqual(first.text, "ok")
            record = SessionStore(settings.resolved_session_store_path()).get("conv-1")
            self.assertIsNotNone(record)
            self.assertEqual(record.provider_session_id, "codex-session-1")

            second = runner.prepare(ChatRequest(provider="codex", prompt="again", conversation_id="conv-1"), stream=False)
            self.assertTrue(second.session_resumed)
            self.assertEqual(second.command[:3], [str(fake_codex), "exec", "resume"])
            self.assertIn("codex-session-1", second.command)

    def test_codex_fallback_models_retry_after_primary_model_failure(self) -> None:
        fake_codex = temporary_executable(
            PACKAGE_ROOT,
            "fake-codex-model-fallback",
            """
model = sys.argv[sys.argv.index('--model') + 1] if '--model' in sys.argv else ''
if model == 'gpt-spark':
    sys.stderr.write('primary model unavailable\\n')
    raise SystemExit(1)
print('fallback-ok:' + model)
""",
        )
        settings = Settings(
            workspace_root=PACKAGE_ROOT,
            codex_bin=str(fake_codex),
            fallback_providers=("codex",),
            codex_fallback_models=("gpt-5.5-low",),
        )
        runner = ChatRunner(settings)

        result = runner.run(
            ChatRequest(
                provider="codex",
                model="gpt-spark",
                prompt="hello",
                fallback=True,
                safe_guard=False,
            )
        )

        self.assertEqual(result.text, "fallback-ok:gpt-5.5-low")
        self.assertEqual(result.model, "gpt-5.5-low")
        self.assertEqual(len(result.fallback_attempts), 1)

    def test_persistent_conversation_rejects_provider_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as tmp:
            store = SessionStore(Path(tmp) / "sessions.json")
            store.save(
                conversation_id="conv-1",
                provider="claude",
                provider_session_id="claude-session-1",
                cwd=str(PACKAGE_ROOT),
                codex_sandbox=None,
            )
            runner = ChatRunner(Settings(workspace_root=PACKAGE_ROOT, session_store_path=store.path))
            with self.assertRaises(RequestError):
                runner.prepare(ChatRequest(provider="codex", prompt="again", conversation_id="conv-1"), stream=False)

    def test_persist_false_leaves_existing_conversation_ephemeral(self) -> None:
        with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as tmp:
            store = SessionStore(Path(tmp) / "sessions.json")
            store.save(
                conversation_id="conv-1",
                provider="claude",
                provider_session_id="claude-session-1",
                cwd=str(PACKAGE_ROOT),
                codex_sandbox=None,
            )
            runner = ChatRunner(Settings(workspace_root=PACKAGE_ROOT, session_store_path=store.path))
            prepared = runner.prepare(ChatRequest(provider="claude", prompt="again", conversation_id="conv-1", persist=False), stream=False)
            self.assertFalse(prepared.session_resumed)
            self.assertNotIn("--resume", prepared.command)
            self.assertIn("--no-session-persistence", prepared.command)

    def test_rate_limiter_blocks_after_fixed_window_capacity(self) -> None:
        limiter = InMemoryRateLimiter(Settings(workspace_root=PACKAGE_ROOT, rate_limit_requests=2, rate_limit_window_seconds=60))
        self.assertTrue(limiter.check("ip:1", now=10).allowed)
        second = limiter.check("ip:1", now=11)
        self.assertTrue(second.allowed)
        self.assertEqual(second.remaining, 0)
        third = limiter.check("ip:1", now=12)
        self.assertFalse(third.allowed)
        self.assertEqual(third.reset_epoch, 60)
        self.assertTrue(limiter.check("ip:1", now=61).allowed)

    def test_rate_limit_bucket_prefers_authorization_hash(self) -> None:
        auth_bucket = bucket_key_for_request(authorization="Bearer secret", client_ip="127.0.0.1")
        ip_bucket = bucket_key_for_request(authorization=None, client_ip="127.0.0.1")
        self.assertTrue(auth_bucket.startswith("auth:"))
        self.assertEqual(ip_bucket, "ip:127.0.0.1")
        self.assertNotIn("secret", auth_bucket)

    def test_rate_limiter_capacity_is_exact_under_concurrent_checks(self) -> None:
        limiter = InMemoryRateLimiter(Settings(workspace_root=PACKAGE_ROOT, rate_limit_requests=5, rate_limit_window_seconds=60))

        with ThreadPoolExecutor(max_workers=20) as executor:
            decisions = list(executor.map(lambda _index: limiter.check("ip:concurrent", now=10), range(20)))

        self.assertEqual(sum(1 for decision in decisions if decision.allowed), 5)
        self.assertEqual(sum(1 for decision in decisions if not decision.allowed), 15)

    def test_session_store_parallel_saves_preserve_valid_json(self) -> None:
        with tempfile.TemporaryDirectory(dir=PACKAGE_ROOT) as tmp:
            store = SessionStore(Path(tmp) / "sessions.json")

            def save(index: int) -> None:
                store.save(
                    conversation_id=f"conv-{index}",
                    provider="claude",
                    provider_session_id=f"session-{index}",
                    cwd=str(PACKAGE_ROOT),
                    codex_sandbox=None,
                )

            with ThreadPoolExecutor(max_workers=12) as executor:
                list(executor.map(save, range(20)))

            data = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 20)
            self.assertEqual(data["conv-19"]["provider_session_id"], "session-19")


if __name__ == "__main__":
    unittest.main()
