# =========================================
# File: agent_chat_api/tests/support.py
# Purpose:
#   - Provides shared test utilities for exercising the HTTP handler and fake
#     provider binaries without opening sockets or calling real CLIs.
# What This File Does:
#   - Defines package paths used by all Agent Chat API tests and ensures the
#     package parent is importable when tests run directly.
#   - Implements `FakeSocket`, which gives `BaseHTTPRequestHandler` byte streams
#     for in-process request/response testing.
#   - Creates temporary executable Python scripts under `.test-bin` so provider
#     tests can simulate Codex/Claude success, refusal, stderr, and exit codes.
# Main Classes:
#   - `FakeSocket`: Minimal socket stand-in with readable request bytes and a
#     writable response buffer.
# Main Methods:
#   - `makefile()`: Returns the read or write buffer expected by
#     `BaseHTTPRequestHandler`.
#   - `sendall()` / `response_bytes()`: Capture bytes the handler writes.
# Main Functions:
#   - `run_raw_http_request()`: Invokes a handler with exact request bytes for
#     malformed-header and framing tests that cannot use a header dictionary.
#   - `run_handler_raw()`: Builds a raw HTTP request, invokes the handler class,
#     and returns status, headers, and body text.
#   - `run_handler()`: Same as raw mode, but JSON-decodes the response body.
#   - `temporary_executable()` / `remove_test_bin()`: Create and clean fake
#     provider binaries used by provider/session/server tests.
# Used By:
#   - `agent_chat_api/tests/test_config.py` imports `PACKAGE_ROOT` from this file.
#   - `agent_chat_api/tests/test_providers.py` imports `PACKAGE_ROOT`, `remove_test_bin`, `temporary_executable` from this file.
#   - `agent_chat_api/tests/test_reporter.py` imports `PACKAGE_ROOT` from this file.
#   - `agent_chat_api/tests/test_server.py` imports `PACKAGE_ROOT`, `remove_test_bin`, `run_handler`, `run_handler_raw`, `temporary_executable` from this file.
#   - `agent_chat_api/tests/test_service_assets.py` imports `PACKAGE_ROOT` from this file.
#   - `agent_chat_api/tests/test_service_manifest.py` imports `PACKAGE_ROOT` from this file.
#   - `agent_chat_api/tests/test_sessions_rate.py` imports `PACKAGE_ROOT`, `remove_test_bin`, `temporary_executable` from this file.
# Reader Note:
#   - Keep these helpers small and predictable; many tests depend on exact fake
#     HTTP and fake executable behavior.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class FakeSocket:
    def __init__(self, request: bytes) -> None:
        self._reader = io.BytesIO(request)
        self._writer = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        if "r" in mode:
            return self._reader
        return self._writer

    def sendall(self, data: bytes) -> None:
        self._writer.write(data)

    def response_bytes(self) -> bytes:
        return self._writer.getvalue()


def run_raw_http_request(handler_cls: type, raw_request: bytes) -> tuple[int, dict[str, str], str]:
    sock = FakeSocket(raw_request)
    handler_cls(sock, ("127.0.0.1", 12345), object())
    raw_response = sock.response_bytes()
    header_block, _, response_body = raw_response.partition(b"\r\n\r\n")
    header_lines = header_block.splitlines()
    status_line = header_lines[0].decode("iso-8859-1")
    status = int(status_line.split(" ")[1])
    response_headers: dict[str, str] = {}
    for line in header_lines[1:]:
        decoded = line.decode("iso-8859-1")
        name, separator, value = decoded.partition(":")
        if separator:
            response_headers[name.strip().lower()] = value.strip()
    return status, response_headers, response_body.decode("utf-8")


def run_handler_raw(
    handler_cls: type,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    body = b""
    supplied_headers = headers or {}
    supplied_names = {name.lower() for name in supplied_headers}
    header_lines = [f"{method} {path} HTTP/1.1"]
    if "host" not in supplied_names:
        header_lines.append("Host: 127.0.0.1")
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        if "content-type" not in supplied_names:
            header_lines.append("Content-Type: application/json")
        header_lines.append(f"Content-Length: {len(body)}")
    for name, value in supplied_headers.items():
        header_lines.append(f"{name}: {value}")
    raw_request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + body
    return run_raw_http_request(handler_cls, raw_request)


def run_handler(
    handler_cls: type,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    status, _headers, body = run_handler_raw(handler_cls, method, path, payload, headers)
    return status, json.loads(body or "{}")


def temporary_executable(root: Path, name: str, body: str) -> Path:
    bin_dir = root / ".test-bin"
    bin_dir.mkdir(exist_ok=True)
    return write_executable(
        bin_dir / name,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        + body.lstrip(),
    )


def remove_test_bin(root: Path) -> None:
    bin_dir = root / ".test-bin"
    if not bin_dir.exists():
        return
    for path in sorted(bin_dir.iterdir(), reverse=True):
        if path.is_file():
            path.unlink()
    os.rmdir(bin_dir)
