# =========================================
# File: agent_chat_api/server.py
# Purpose:
#   - Implements the HTTP surface for the standalone chat API wrapper, including
#     health/provider endpoints, native chat requests, OpenAI-compatible chat
#     completions, rate limiting, JSON responses, and SSE streaming.
# What This File Does:
#   - Builds a request-handler class bound to a `Settings`, `ChatRunner`, and
#     in-memory rate limiter.
#   - Serves public health, authenticated readiness/capabilities, provider
#     status, native `/api/chat`, and `/v1/chat/completions` routes.
#   - Enforces bearer-token auth when configured and rate-limits non-dry-run
#     requests by authorization header or client IP.
#   - Converts native provider results into either the wrapper JSON format or an
#     OpenAI-style chat completion response.
#   - Streams both native provider events and OpenAI-compatible chunks over
#     server-sent events.
# Main Classes:
#   - `AgentChatHandler`: `BaseHTTPRequestHandler` subclass that routes every
#     supported endpoint and owns response serialization.
#   - `ReusableThreadingHTTPServer`: Threaded HTTP server with address reuse
#     enabled for local development restarts.
# Main Methods:
#   - `do_GET()` / `do_POST()`: Route health, provider, native chat, and
#     OpenAI-compatible requests.
#   - `_handle_chat()` / `_handle_openai_chat()`: Parse payloads, apply rate
#     limits, run or stream provider work, and format errors consistently.
#   - `_handle_api_stream()` / `_handle_openai_stream()`: Write SSE responses for
#     native events and OpenAI-style deltas.
#   - `_read_json()`, `_write_json()`, `_write_sse_*()`: Low-level body parsing
#     and response helpers.
# Main Functions:
#   - `create_app()`: Binds runtime objects onto a concrete handler class.
#   - `_openai_role_chunk()`, `_openai_delta_chunk()`, `_openai_done_chunk()`:
#     Shape SSE chunks for clients expecting OpenAI chat-completion streaming.
#   - `run_server()`: Creates the threaded server, prints bind/workspace
#     information, and enters `serve_forever()`.
# Used By:
#   - `agent_chat_api/app.py` imports `run_server` from this file.
#   - `agent_chat_api/tests/test_server.py` imports `create_app` from this file.
# Reader Note:
#   - Route and payload changes here affect API compatibility directly. Check
#     both native `/api/chat` tests and OpenAI-compatible `/v1/chat/completions`
#     tests when editing this file.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import hmac
from contextlib import closing
import ipaddress
import json
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .providers import ChatRunner, ProviderError, RequestError, request_from_payload
from .rate_limit import InMemoryRateLimiter, bucket_key_for_request


def create_app(settings: Settings) -> type[BaseHTTPRequestHandler]:
    return type(
        "Handler",
        (AgentChatHandler,),
        {
            "settings": settings,
            "runner": ChatRunner(settings),
            "rate_limiter": InMemoryRateLimiter(settings),
        },
    )


class AgentChatHandler(BaseHTTPRequestHandler):
    server_version = "AgentChatAPI/0.1"
    settings: Settings
    runner: ChatRunner
    rate_limiter: InMemoryRateLimiter

    def do_GET(self) -> None:
        if not self._check_request_host():
            return
        path = urlsplit(self.path).path
        if path in {"/health", "/api/health"}:
            self._write_json(HTTPStatus.OK, self._health_payload())
            return
        if path == "/api/readiness":
            if not self._check_readiness_authentication():
                return
            payload = self._health_payload()
            payload["authentication_verified"] = True
            self._write_json(HTTPStatus.OK, payload)
            return
        if path in {"/providers", "/api/providers"}:
            self._write_json(HTTPStatus.OK, {"providers": self.runner.providers()})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._check_request_host() or not self._check_request_origin():
            return
        if not self._check_post_authentication():
            return
        path = urlsplit(self.path).path
        if path == "/api/chat":
            if not self._check_json_content_type():
                return
            self._handle_chat()
            return
        if path == "/v1/chat/completions":
            if not self._check_json_content_type():
                return
            self._handle_openai_chat()
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _health_payload(self) -> dict[str, Any]:
        providers = self.runner.providers()
        default_provider = self.settings.default_provider
        default_status = providers.get(default_provider)
        provider_ready = bool(default_status.get("available")) if isinstance(default_status, dict) else False
        bearer_configured = bool((self.settings.api_key or "").strip())
        authentication_configured = bearer_configured or self.settings.allow_unauthenticated_posts
        ready = provider_ready and authentication_configured
        if not authentication_configured:
            reason_code = "authentication_not_configured"
        elif not provider_ready:
            reason_code = "default_provider_unavailable"
        else:
            reason_code = "ready"
        return {
            "ok": ready,
            "service_reachable": True,
            "reason_code": reason_code,
            "authentication_configured": authentication_configured,
            "authentication_required": bearer_configured or not self.settings.allow_unauthenticated_posts,
            "workspace_root": str(self.settings.workspace_root),
            "default_provider": default_provider,
            "provider_fallback_enabled": self.settings.provider_fallback_enabled,
            "provider_status_schema_version": 1,
            "fallback_providers": list(self.settings.fallback_providers),
            "providers": providers,
        }

    def _check_request_host(self) -> bool:
        values = self.headers.get_all("Host") or []
        if len(values) == 1 and _is_loopback_authority(values[0]):
            return True
        self._write_json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "host_not_allowed"})
        return False

    def _check_request_origin(self) -> bool:
        values = self.headers.get_all("Origin") or []
        if not values:
            return True
        if len(values) == 1 and _is_loopback_origin(values[0]):
            return True
        self._write_json(HTTPStatus.FORBIDDEN, {"error": "origin_not_allowed"})
        return False

    def _check_post_authentication(self) -> bool:
        return self._check_bearer_authentication(
            allow_unauthenticated=self.settings.allow_unauthenticated_posts
        )

    def _check_readiness_authentication(self) -> bool:
        return self._check_bearer_authentication(allow_unauthenticated=False)

    def _check_bearer_authentication(self, *, allow_unauthenticated: bool) -> bool:
        api_key = (self.settings.api_key or "").strip()
        if not api_key:
            if allow_unauthenticated:
                return True
            self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "authentication_not_configured"})
            return False

        values = self.headers.get_all("Authorization") or []
        expected = f"Bearer {api_key}"
        if len(values) == 1 and hmac.compare_digest(values[0], expected):
            return True
        self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _check_json_content_type(self) -> bool:
        values = self.headers.get_all("Content-Type") or []
        content_type = self.headers.get_content_type() if len(values) == 1 else ""
        charset = self.headers.get_content_charset() if len(values) == 1 else None
        if content_type == "application/json" and (charset is None or charset.lower() == "utf-8"):
            return True
        self._write_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "unsupported_media_type"})
        return False

    def _handle_chat(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        try:
            request = request_from_payload(payload, self.settings.default_provider)
            if not self._check_rate_limit(request):
                return
            if request.stream:
                self._handle_api_stream(request)
                return
            result = self.runner.run(request)
        except RequestError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except ProviderError as exc:
            self._write_json(exc.status_code, {"error": exc.public_payload()})
            return
        self._write_json(HTTPStatus.OK, result.public_dict(include_raw=request.include_raw))

    def _handle_openai_chat(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        try:
            request = request_from_payload(payload, self.settings.default_provider)
            if not self._check_rate_limit(request):
                return
            if request.stream:
                self._handle_openai_stream(request, payload)
                return
            result = self.runner.run(request)
        except RequestError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return
        except ProviderError as exc:
            self._write_json(
                exc.status_code,
                {
                    "error": {
                        "message": str(exc),
                        "type": exc.code,
                        "retryable": exc.retryable,
                        "details": exc.details,
                    }
                },
            )
            return

        response = self._openai_response(payload, result)
        self._write_json(HTTPStatus.OK, response)

    def _openai_response(self, payload: dict[str, Any], result: Any) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or result.model or result.provider,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text},
                    "finish_reason": "stop",
                }
            ],
            "provider": result.provider,
            "provider_meta": {
                "request_id": result.request_id,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "cwd": result.cwd,
                "dry_run": result.dry_run,
                "warnings": result.warnings,
                "fallback": {
                    "used": bool(result.fallback_attempts),
                    "attempts": result.fallback_attempts,
                    "final": result.final_fallback,
                },
                "audit_events": result.audit_events,
                "provider_status": result.public_dict()["provider_status"],
            },
        }

    def _handle_api_stream(self, request: Any) -> None:
        self._write_sse_headers()
        with closing(self.runner.stream(request)) as events:
            for event in events:
                self._write_sse(event.event, event.data)
                if self.close_connection:
                    return

    def _handle_openai_stream(self, request: Any, payload: dict[str, Any]) -> None:
        self._write_sse_headers()
        stream_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        model = payload.get("model") or request.model or request.provider or self.settings.default_provider
        role_sent = False
        with closing(self.runner.stream(request)) as events:
            for event in events:
                if event.event == "meta":
                    self._write_sse_data(_openai_role_chunk(stream_id, created, model, event.data))
                    role_sent = True
                    if self.close_connection:
                        return
                    continue
                if event.event == "delta":
                    if not role_sent:
                        self._write_sse_data(_openai_role_chunk(stream_id, created, model, {}))
                        role_sent = True
                    self._write_sse_data(_openai_delta_chunk(stream_id, created, model, event.data.get("text", "")))
                    if self.close_connection:
                        return
                    continue
                if event.event == "error":
                    self._write_sse_data({"error": {"message": event.data.get("error", "provider error"),
                        "type": event.data.get("code", "provider_error"), "details": event.data}})
                    self._write_sse_raw("[DONE]")
                    return
                if event.event == "done":
                    self._write_sse_data(_openai_done_chunk(stream_id, created, model, event.data))
                    self._write_sse_raw("[DONE]")
                    return
        self._write_sse_raw("[DONE]")

    def _check_rate_limit(self, request: Any) -> bool:
        if request.dry_run:
            return True
        authorization = self.headers.get("Authorization")
        client_ip = self.client_address[0] if self.client_address else "unknown"
        decision = self.rate_limiter.check(bucket_key_for_request(authorization=authorization, client_ip=client_ip))
        if decision.allowed:
            return True
        self._write_json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "rate_limited", "reset_epoch": decision.reset_epoch},
            headers=decision.headers(),
        )
        return False

    def _read_json(self) -> dict[str, Any] | None:
        if self.headers.get_all("Transfer-Encoding"):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_transfer_encoding"})
            return None
        length_values = self.headers.get_all("Content-Length") or []
        if not length_values:
            self._write_json(HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"})
            return None
        if len(length_values) != 1:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return None
        length_header = length_values[0]
        if not length_header or not length_header.isascii() or not length_header.isdecimal():
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return None
        try:
            length = int(length_header)
        except ValueError:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return None
        if length > self.settings.max_request_body_bytes:
            self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_body_too_large"})
            return None
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            self.close_connection = True
            self._write_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "request_timeout"})
            return None
        if len(raw) != length:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete_request_body"})
            return None
        try:
            data = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return None
        if not isinstance(data, dict):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
            return None
        return data

    def _write_json(self, status: int | HTTPStatus, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_sse_headers(self) -> None:
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_sse(self, event: str, data: dict[str, Any]) -> None:
        if self._write_bytes(f"event: {event}\n".encode("utf-8")):
            self._write_sse_raw(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def _write_sse_data(self, data: dict[str, Any]) -> None:
        self._write_sse_raw(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def _write_sse_raw(self, data: str) -> bool:
        for line in data.splitlines() or [""]:
            if not self._write_bytes(f"data: {line}\n".encode("utf-8")):
                return False
        return self._write_bytes(b"\n")

    def _write_bytes(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            return False


def _openai_role_chunk(stream_id: str, created: int, model: str, provider_meta: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    if provider_meta:
        payload["provider"] = provider_meta.get("provider")
        payload["provider_meta"] = provider_meta
    return payload


def _openai_delta_chunk(stream_id: str, created: int, model: str, text: str) -> dict[str, Any]:
    return {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _openai_done_chunk(stream_id: str, created: int, model: str, provider_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "provider_meta": provider_meta,
    }


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        max_concurrent_requests: int = 8,
        socket_io_timeout_seconds: int = 15,
        bind_and_activate: bool = True,
    ) -> None:
        self.max_concurrent_requests = max(1, min(int(max_concurrent_requests), 128))
        self.socket_io_timeout_seconds = max(1, min(int(socket_io_timeout_seconds), 300))
        self._request_slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        super().__init__(server_address, request_handler_class, bind_and_activate=bind_and_activate)

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.socket_io_timeout_seconds)
        return request, client_address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            self._reject_overloaded_request(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    @staticmethod
    def _reject_overloaded_request(request: Any) -> None:
        body = b'{"error":"server_busy"}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n"
            + b"Retry-After: 1\r\n"
            + b"\r\n"
            + body
        )
        try:
            request.sendall(response)
        except OSError:
            return


def _is_literal_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_authority(authority: str) -> bool:
    value = authority.strip()
    if not value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError:
        return False
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return False
    return _is_literal_loopback_host(parsed.hostname)


def _is_loopback_origin(origin: str) -> bool:
    value = origin.strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return False
    return _is_literal_loopback_host(parsed.hostname)


def run_server(settings: Settings) -> None:
    handler = create_app(settings)
    httpd = ReusableThreadingHTTPServer(
        (settings.host, settings.port),
        handler,
        max_concurrent_requests=settings.max_concurrent_requests,
        socket_io_timeout_seconds=settings.socket_io_timeout_seconds,
    )
    print(f"agent-chat-api listening on http://{settings.host}:{settings.port}")
    print(f"workspace_root={settings.workspace_root}")
    httpd.serve_forever()
