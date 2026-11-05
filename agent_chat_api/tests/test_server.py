# =========================================
# File: agent_chat_api/tests/test_server.py
# Purpose:
#   - Verifies the HTTP handler created by `agent_chat_api.server.create_app`
#     across health, provider, native chat, OpenAI-compatible, streaming, auth,
#     fallback, and rate-limit paths.
# What This File Does:
#   - Runs the handler in-process through fake socket helpers rather than
#     opening a real listening port.
#   - Checks JSON response bodies, status codes, query-string routing, bearer
#     token enforcement, dry-run command output, native SSE events, and
#     OpenAI-style streaming chunks.
#   - Uses fake provider executables to test live execution, fallback from Codex
#     refusal to Claude, and rate-limit behavior.
# Main Classes:
#   - `ServerTests`: Full HTTP-surface regression case for the local chat API
#     handler.
# Main Methods:
#   - Health/provider tests protect GET route compatibility.
#   - Native `/api/chat` tests protect dry-run, fallback, auth, streaming, and
#     throttling behavior.
#   - `/v1/chat/completions` tests protect OpenAI-compatible response and SSE
#     chunk shapes.
# Used By:
#   - This file also runs directly through its own `__main__` entrypoint path.
# Reader Note:
#   - Keep these tests aligned with route changes in `server.py`; small response
#     shape changes can break clients even when provider execution still works.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import http.client
import socket
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

try:
    from agent_chat_api.tests.support import PACKAGE_ROOT, remove_test_bin, run_handler, run_handler_raw, run_raw_http_request, temporary_executable
except ModuleNotFoundError:
    from support import PACKAGE_ROOT, remove_test_bin, run_handler, run_handler_raw, run_raw_http_request, temporary_executable

from agent_chat_api.config import Settings
from agent_chat_api.server import ReusableThreadingHTTPServer, create_app


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.codex_bin = temporary_executable(
            PACKAGE_ROOT,
            "fake-codex-server-default",
            """
if '--version' in sys.argv:
    print('codex test')
    raise SystemExit(0)
if sys.argv[1:] == ['login', 'status']:
    print('logged in')
    raise SystemExit(0)
print('codex test response')
""",
        )
        self.claude_bin = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-server-default",
            """
if '--version' in sys.argv:
    print('claude test')
    raise SystemExit(0)
if sys.argv[1:] == ['auth', 'status']:
    print('{"loggedIn":true}')
    raise SystemExit(0)
print('{"result":"claude test response"}')
""",
        )
        self.settings = Settings(
            workspace_root=PACKAGE_ROOT,
            codex_bin=str(self.codex_bin),
            claude_bin=str(self.claude_bin),
            allow_unauthenticated_posts=True,
        )
        self.handler_cls = create_app(self.settings)

    def tearDown(self) -> None:
        remove_test_bin(PACKAGE_ROOT)

    def test_health_reports_workspace_and_providers(self) -> None:
        status, payload = run_handler(self.handler_cls, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["workspace_root"], str(PACKAGE_ROOT))
        self.assertIn("codex", payload["providers"])
        self.assertIn("claude", payload["providers"])

    def test_health_accepts_query_string(self) -> None:
        status, payload = run_handler(self.handler_cls, "GET", "/health?check=1")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_health_separates_process_liveness_from_default_provider_readiness(self) -> None:
        handler_cls = create_app(
            Settings(
                workspace_root=PACKAGE_ROOT,
                default_provider="codex",
                codex_bin=str(PACKAGE_ROOT / "missing-codex"),
                claude_bin=str(PACKAGE_ROOT / "missing-claude"),
                allow_unauthenticated_posts=True,
            )
        )

        status, payload = run_handler(handler_cls, "GET", "/health")

        self.assertEqual(status, 200)
        self.assertTrue(payload["service_reachable"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason_code"], "default_provider_unavailable")

    def test_health_fails_closed_when_post_authentication_is_not_configured(self) -> None:
        handler_cls = create_app(
            Settings(
                workspace_root=PACKAGE_ROOT,
                codex_bin=str(self.codex_bin),
                claude_bin=str(self.claude_bin),
            )
        )

        status, payload = run_handler(handler_cls, "GET", "/health")

        self.assertEqual(status, 200)
        self.assertTrue(payload["service_reachable"])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["authentication_configured"])
        self.assertEqual(payload["reason_code"], "authentication_not_configured")

    def test_readiness_requires_the_exact_configured_bearer(self) -> None:
        handler_cls = create_app(
            Settings(
                workspace_root=PACKAGE_ROOT,
                codex_bin=str(self.codex_bin),
                claude_bin=str(self.claude_bin),
                api_key="readiness-secret",
            )
        )

        missing_status, missing_payload = run_handler(handler_cls, "GET", "/api/readiness")
        wrong_status, wrong_payload = run_handler(
            handler_cls,
            "GET",
            "/api/readiness",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        ready_status, ready_payload = run_handler(
            handler_cls,
            "GET",
            "/api/readiness",
            headers={"Authorization": "Bearer readiness-secret"},
        )

        self.assertEqual((missing_status, missing_payload), (401, {"error": "unauthorized"}))
        self.assertEqual((wrong_status, wrong_payload), (401, {"error": "unauthorized"}))
        self.assertEqual(ready_status, 200)
        self.assertTrue(ready_payload["ok"])
        self.assertTrue(ready_payload["authentication_verified"])

    def test_readiness_does_not_use_the_unauthenticated_development_bypass(self) -> None:
        handler_cls = create_app(
            Settings(
                workspace_root=PACKAGE_ROOT,
                codex_bin=str(self.codex_bin),
                claude_bin=str(self.claude_bin),
                allow_unauthenticated_posts=True,
            )
        )

        status, payload = run_handler(handler_cls, "GET", "/api/readiness")

        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": "authentication_not_configured"})

    def test_health_reports_authentication_required_when_a_bearer_is_configured(self) -> None:
        handler_cls = create_app(
            Settings(
                workspace_root=PACKAGE_ROOT,
                codex_bin=str(self.codex_bin),
                claude_bin=str(self.claude_bin),
                api_key="configured-secret",
                allow_unauthenticated_posts=True,
            )
        )

        status, payload = run_handler(handler_cls, "GET", "/health")

        self.assertEqual(status, 200)
        self.assertTrue(payload["authentication_required"])

    def test_simple_chat_dry_run_returns_claude_command(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat",
            {"provider": "claude", "prompt": "hello", "dry_run": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"], "claude")
        self.assertTrue(payload["dry_run"])
        self.assertIn("request_id", payload)
        self.assertEqual(payload["cwd"], str(PACKAGE_ROOT))
        self.assertIn("--tools ''", payload["command"])

    def test_simple_chat_defaults_to_codex(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat",
            {"prompt": "hello", "dry_run": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"], "codex")
        self.assertTrue(payload["dry_run"])
        self.assertIn("codex", payload["command"])

    def test_api_chat_falls_back_from_codex_refusal_to_claude(self) -> None:
        fake_codex = temporary_executable(
            PACKAGE_ROOT,
            "fake-codex-api-refusal",
            """
print("Sorry, I cannot do that.")
""",
        )
        fake_claude = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-api-fallback",
            """
print('{"result":"api fallback ok"}')
""",
        )
        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = create_app(
                Settings(
                    workspace_root=PACKAGE_ROOT,
                    codex_bin=str(fake_codex),
                    claude_bin=str(fake_claude),
                    audit_log_path=Path(tmp) / "provider-audit.jsonl",
                    allow_unauthenticated_posts=True,
                )
            )
            status, payload = run_handler(handler_cls, "POST", "/api/chat", {"prompt": "hello"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["text"], "api fallback ok")
        self.assertTrue(payload["fallback"]["used"])
        self.assertEqual(payload["fallback"]["attempts"][0]["provider"], "codex")
        self.assertEqual(payload["audit_events"][0]["reason"], "refusal_response")

    def test_api_chat_returns_typed_non_success_when_all_fallbacks_fail(self) -> None:
        fake_codex = temporary_executable(
            PACKAGE_ROOT,
            "fake-codex-api-all-refuse",
            """
print("I cannot help with that.")
""",
        )
        fake_claude = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-api-all-refuse",
            """
print('{"result":"I cannot assist with that request."}')
""",
        )
        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = create_app(
                Settings(
                    workspace_root=PACKAGE_ROOT,
                    codex_bin=str(fake_codex),
                    claude_bin=str(fake_claude),
                    audit_log_path=Path(tmp) / "provider-audit.jsonl",
                    allow_unauthenticated_posts=True,
                )
            )
            status, payload = run_handler(
                handler_cls,
                "POST",
                "/api/chat",
                {"provider": "codex", "prompt": "hello", "fallback": True},
            )

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "provider_fallback_exhausted")
        self.assertFalse(payload["error"]["retryable"])
        self.assertEqual(
            [item["provider"] for item in payload["error"]["details"]["attempts"]],
            ["codex", "claude"],
        )

    def test_openai_compatible_endpoint_accepts_provider_prefixed_model(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/v1/chat/completions",
            {
                "model": "codex:gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "dry_run": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["choices"][0]["message"]["role"], "assistant")
        self.assertTrue(payload["provider_meta"]["dry_run"])
        self.assertIn("request_id", payload["provider_meta"])

    def test_post_route_accepts_query_string(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat?trace=1",
            {"provider": "claude", "prompt": "hello", "dry_run": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider"], "claude")

    def test_auth_blocks_missing_bearer_token(self) -> None:
        handler_cls = create_app(Settings(workspace_root=PACKAGE_ROOT, api_key="secret"))
        status, payload = run_handler(handler_cls, "POST", "/api/chat", {"prompt": "hello", "dry_run": True})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_post_fails_closed_when_no_bearer_token_is_configured(self) -> None:
        handler_cls = create_app(Settings(workspace_root=PACKAGE_ROOT))

        status, payload = run_handler(handler_cls, "POST", "/api/chat", {"prompt": "hello", "dry_run": True})

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "authentication_not_configured")

    def test_explicit_development_mode_allows_unauthenticated_post(self) -> None:
        handler_cls = create_app(
            Settings(workspace_root=PACKAGE_ROOT, allow_unauthenticated_posts=True)
        )

        status, payload = run_handler(handler_cls, "POST", "/api/chat", {"prompt": "hello", "dry_run": True})

        self.assertEqual(status, 200)
        self.assertTrue(payload["dry_run"])

    def test_rejects_non_loopback_host_header(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat",
            {"prompt": "hello", "dry_run": True},
            headers={"Host": "attacker.example"},
        )

        self.assertEqual(status, 421)
        self.assertEqual(payload["error"], "host_not_allowed")

    def test_rejects_non_loopback_browser_origin(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat",
            {"prompt": "hello", "dry_run": True},
            headers={"Origin": "https://attacker.example"},
        )

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "origin_not_allowed")

    def test_accepts_loopback_browser_origin(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat",
            {"prompt": "hello", "dry_run": True},
            headers={"Origin": "http://localhost:3000"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["dry_run"])

    def test_rejects_non_json_chat_payload(self) -> None:
        status, payload = run_handler(
            self.handler_cls,
            "POST",
            "/api/chat",
            {"prompt": "hello", "dry_run": True},
            headers={"Content-Type": "text/plain"},
        )

        self.assertEqual(status, 415)
        self.assertEqual(payload["error"], "unsupported_media_type")

    def test_post_requires_content_length(self) -> None:
        status, _headers, body = run_raw_http_request(
            self.handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b"{}",
        )

        self.assertEqual(status, 411)
        self.assertEqual(json.loads(body), {"error": "content_length_required"})

    def test_post_rejects_duplicate_content_length(self) -> None:
        status, _headers, body = run_raw_http_request(
            self.handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n"
            b"Content-Length: 2\r\n"
            b"\r\n"
            b"{}",
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "invalid_content_length"})

    def test_post_rejects_negative_content_length(self) -> None:
        status, _headers, body = run_raw_http_request(
            self.handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: -1\r\n"
            b"\r\n",
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "invalid_content_length"})

    def test_post_rejects_transfer_encoding(self) -> None:
        status, _headers, body = run_raw_http_request(
            self.handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Length: 2\r\n"
            b"\r\n"
            b"{}",
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "unsupported_transfer_encoding"})

    def test_post_rejects_body_larger_than_configured_limit(self) -> None:
        handler_cls = create_app(
            Settings(
                workspace_root=PACKAGE_ROOT,
                allow_unauthenticated_posts=True,
                max_request_body_bytes=16,
            )
        )
        status, _headers, body = run_raw_http_request(
            handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 17\r\n"
            b"\r\n"
            b"{\"12345678901\":0}",
        )

        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {"error": "request_body_too_large"})

    def test_post_rejects_incomplete_body(self) -> None:
        status, _headers, body = run_raw_http_request(
            self.handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 8\r\n"
            b"\r\n"
            b"{}",
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "incomplete_request_body"})

    def test_post_rejects_invalid_utf8_without_internal_error_details(self) -> None:
        status, _headers, body = run_raw_http_request(
            self.handler_cls,
            b"POST /api/chat HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 1\r\n"
            b"\r\n"
            b"\xff",
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "invalid_json"})

    def test_auth_accepts_expected_bearer_token(self) -> None:
        handler_cls = create_app(Settings(workspace_root=PACKAGE_ROOT, api_key="secret"))
        status, payload = run_handler(
            handler_cls,
            "POST",
            "/api/chat",
            {"prompt": "hello", "dry_run": True},
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["dry_run"])

    def test_api_chat_streams_sse_events(self) -> None:
        fake_claude = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-stream",
            """
print('{"type":"content_block_delta","delta":{"text":"hello"}}')
print('{"type":"result","result":"hello","session_id":"claude-session-1"}')
""",
        )
        handler_cls = create_app(
            Settings(workspace_root=PACKAGE_ROOT, claude_bin=str(fake_claude), allow_unauthenticated_posts=True)
        )
        status, headers, body = run_handler_raw(
            handler_cls,
            "POST",
            "/api/chat",
            {"provider": "claude", "prompt": "hello", "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertIn("event: meta", body)
        self.assertIn("event: delta", body)
        self.assertIn("hello", body)
        self.assertIn("event: done", body)

    def test_openai_compatible_stream_returns_sse_chunks(self) -> None:
        fake_claude = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-openai-stream",
            """
print('{"type":"content_block_delta","delta":{"text":"hello"}}')
print('{"type":"result","result":"hello","session_id":"claude-session-1"}')
""",
        )
        handler_cls = create_app(
            Settings(workspace_root=PACKAGE_ROOT, claude_bin=str(fake_claude), allow_unauthenticated_posts=True)
        )
        status, headers, body = run_handler_raw(
            handler_cls,
            "POST",
            "/v1/chat/completions",
            {"provider": "claude", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertIn("chat.completion.chunk", body)
        self.assertIn('"content":"hello"', body)
        self.assertIn("data: [DONE]", body)

    def test_rate_limit_blocks_live_requests_after_limit(self) -> None:
        fake_claude = temporary_executable(
            PACKAGE_ROOT,
            "fake-claude-rate",
            """
print('{"result":"ok","session_id":"claude-session-1"}')
""",
        )
        settings = Settings(
            workspace_root=PACKAGE_ROOT,
            claude_bin=str(fake_claude),
            rate_limit_requests=1,
            allow_unauthenticated_posts=True,
        )
        handler_cls = create_app(settings)
        first_status, first_payload = run_handler(handler_cls, "POST", "/api/chat", {"provider": "claude", "prompt": "hello"})
        second_status, second_headers, second_body = run_handler_raw(handler_cls, "POST", "/api/chat", {"provider": "claude", "prompt": "hello"})
        self.assertEqual(first_status, 200)
        self.assertEqual(first_payload["text"], "ok")
        self.assertEqual(second_status, 429)
        self.assertIn("x-ratelimit-limit", second_headers)
        self.assertIn("rate_limited", second_body)

    def test_dry_run_is_exempt_from_rate_limit(self) -> None:
        settings = Settings(workspace_root=PACKAGE_ROOT, rate_limit_requests=1, allow_unauthenticated_posts=True)
        handler_cls = create_app(settings)
        for _index in range(3):
            status, payload = run_handler(handler_cls, "POST", "/api/chat", {"prompt": "hello", "dry_run": True})
            self.assertEqual(status, 200)
            self.assertTrue(payload["dry_run"])

    def test_unknown_route_returns_404(self) -> None:
        status, payload = run_handler(self.handler_cls, "GET", "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")

    def test_json_write_ignores_client_disconnect(self) -> None:
        class BrokenWriter:
            def write(self, _data: bytes) -> int:
                raise BrokenPipeError("client closed")

        class DummyHandler(self.handler_cls):
            def send_response(self, _code: int, _message: str | None = None) -> None:
                return

            def send_header(self, _keyword: str, _value: str) -> None:
                return

            def end_headers(self) -> None:
                return

        handler = object.__new__(DummyHandler)
        handler.wfile = BrokenWriter()
        handler._write_json(200, {"ok": True})

    def test_sse_write_ignores_client_disconnect(self) -> None:
        class BrokenWriter:
            def write(self, _data: bytes) -> int:
                raise ConnectionResetError("client reset")

        handler = object.__new__(self.handler_cls)
        handler.wfile = BrokenWriter()
        self.assertFalse(handler._write_sse_raw("[DONE]"))

    def test_http_server_applies_socket_io_timeout_to_accepted_connections(self) -> None:
        observed_timeouts: list[float | None] = []

        class TimeoutReportingHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                observed_timeouts.append(self.connection.gettimeout())
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = ReusableThreadingHTTPServer(
            ("127.0.0.1", 0),
            TimeoutReportingHandler,
            max_concurrent_requests=1,
            socket_io_timeout_seconds=2,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(observed_timeouts, [2.0])

    def test_http_server_returns_request_timeout_for_stalled_body(self) -> None:
        settings = Settings(
            workspace_root=PACKAGE_ROOT,
            allow_unauthenticated_posts=True,
            socket_io_timeout_seconds=1,
        )
        server = ReusableThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_app(settings),
            max_concurrent_requests=1,
            socket_io_timeout_seconds=settings.socket_io_timeout_seconds,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
        client.settimeout(3)
        try:
            client.sendall(
                b"POST /api/chat HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 100\r\n"
                b"\r\n"
            )
            chunks: list[bytes] = []
            while chunk := client.recv(4096):
                chunks.append(chunk)
            response = b"".join(chunks)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(json.loads(response.partition(b"\r\n\r\n")[2]), {"error": "request_timeout"})

    def test_http_server_rejects_requests_beyond_concurrency_limit(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        class BlockingHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                nonlocal call_count
                with call_lock:
                    call_count += 1
                    call_number = call_count
                if call_number == 1:
                    first_started.set()
                    release_first.wait(timeout=3)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = ReusableThreadingHTTPServer(
            ("127.0.0.1", 0),
            BlockingHandler,
            max_concurrent_requests=1,
            socket_io_timeout_seconds=2,
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        first_result: list[int] = []

        def run_first_request() -> None:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            first_result.append(response.status)
            response.read()
            connection.close()

        first_thread = threading.Thread(target=run_first_request)
        first_thread.start()
        try:
            self.assertTrue(first_started.wait(timeout=2))
            second = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            second.request("GET", "/")
            response = second.getresponse()
            second_body = response.read()
            second.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(second_body), {"error": "server_busy"})
        finally:
            release_first.set()
            first_thread.join(timeout=3)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(first_result, [200])
        self.assertEqual(call_count, 1)


if __name__ == "__main__":
    unittest.main()
