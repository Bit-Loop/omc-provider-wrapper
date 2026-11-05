# =========================================
# File: agent_chat_api/streaming.py
# Purpose:
#   - Runs a prepared provider command and converts its streaming stdout into
#     server-sent event payloads.
# What This File Does:
#   - Emits an initial `meta` event describing request id, provider, model, cwd,
#     command, warnings, and session state.
#   - Starts the provider subprocess, writes the prepared prompt to stdin, parses
#     each stdout line as JSON when possible, and yields both raw provider events
#     and text deltas.
#   - Redacts provider payloads before streaming them and records the provider
#     session when the stream completes successfully.
# Main Classes:
#   - `StreamEvent`: Small event/data wrapper consumed by the HTTP streaming
#     response layer.
# Main Functions:
#   - `stream_events()`: Orchestrates dry-run handling, subprocess execution,
#     stdout parsing, stderr/error reporting, session recording, and final done
#     events.
#   - `_parse_stream_line()`: Accepts JSONL provider events and treats
#     non-JSON lines as plain text.
#   - `_stream_meta_payload()` / `_stream_done_payload()`: Shape the first and
#     final SSE payloads.
# Used By:
#   - `agent_chat_api.server` uses this when a request asks for streaming output.
# Reader Note:
#   - Stream shape matters to clients. If you change event names or payload keys,
#     update server streaming tests and any client code that consumes SSE.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import codecs
from contextlib import closing
import os
import selectors
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .environment import provider_subprocess_environment
from .parsing import parse_provider_stdout
from .provider_status import collect_provider_status


@dataclass(frozen=True)
class StreamEvent:
    event: str
    data: dict[str, Any]


def stream_events(
    prepared: Any,
    *,
    record_session: Callable[..., None],
    redact_json_value: Callable[[Any], Any],
    redact_text: Callable[[str], str],
    extract_stream_text: Callable[[Any], str],
) -> Iterator[StreamEvent]:
    yield StreamEvent("meta", _stream_meta_payload(prepared))
    if prepared.request.dry_run:
        yield StreamEvent("done", {"request_id": prepared.request_id, "text": ""})
        return

    started = time.monotonic()
    text_parts: list[str] = []
    raw_result: dict[str, Any] | list[Any] | None = None
    raw_lines: list[str] = []
    stderr_parts: list[str] = []
    return_code = 1
    try:
        with closing(_provider_output(prepared)) as output:
            for channel, value in output:
                if channel == "exit":
                    return_code = int(value)
                    continue
                if channel == "stderr":
                    stderr_parts.append(str(value))
                    continue
                raw_line = str(value)
                raw_lines.append(raw_line)
                parsed = _parse_stream_line(raw_line)
                if parsed is None:
                    continue
                event_payload = redact_json_value(parsed) if isinstance(parsed, dict) else {"payload": redact_json_value(parsed)}
                if isinstance(parsed, dict) and parsed.get("type") == "result":
                    raw_result = parsed
                delta = extract_stream_text(parsed)
                yield StreamEvent("provider_event", {"request_id": prepared.request_id, "payload": event_payload})
                if delta:
                    text_parts.append(delta)
                    yield StreamEvent("delta", {"request_id": prepared.request_id, "text": delta})
    except subprocess.TimeoutExpired:
        yield StreamEvent("error", {"request_id": prepared.request_id, "error": "Provider request timed out.", "code": "provider_timeout"})
        return
    except OSError as exc:
        yield StreamEvent("error", {"request_id": prepared.request_id, "error": f"{prepared.provider} failed to start: {exc}"})
        return
    stderr = "".join(stderr_parts)
    text, all_events = parse_provider_stdout(prepared.provider, "".join(raw_lines))
    provider_status = collect_provider_status(provider=prepared.provider,
        requested_model=prepared.request.model, raw_json=all_events,
        text=text, stderr=stderr, exit_code=return_code)
    if provider_status["outcome"] != "completed":
        yield StreamEvent("error", {"request_id": prepared.request_id, "error": redact_text(stderr.strip() or "Provider did not complete the request."),
            "code": provider_status["reason_code"], "exit_code": return_code,
            "provider_status": redact_json_value(provider_status)})
        return
    if return_code != 0:
        detail = stderr.strip() or f"{prepared.provider} exited with {return_code}"
        yield StreamEvent("error", {"request_id": prepared.request_id, "error": redact_text(detail), "exit_code": return_code})
        return

    raw_text = str(raw_result.get("result") or "") if isinstance(raw_result, dict) else ""
    record_session(prepared, stdout="", stderr=stderr, raw_json=all_events)
    done = _stream_done_payload(prepared, text or "".join(text_parts) or raw_text, started)
    done["provider_status"] = redact_json_value(provider_status)
    yield StreamEvent("done", done)


def _provider_output(prepared: Any) -> Iterator[tuple[str, str | int]]:
    """Drain both output pipes while feeding stdin, bounded by one deadline.

    Closing this generator (including client disconnect) kills the process
    group and reaps the provider. Pipe reads never block past the deadline.
    """
    process = subprocess.Popen(prepared.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(prepared.cwd), env=provider_subprocess_environment(),
        start_new_session=True)
    deadline = time.monotonic() + prepared.timeout
    pending = memoryview(prepared.prompt.encode("utf-8"))
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    stdout_buffer = ""
    try:
        with selectors.DefaultSelector() as ready:
            for stream, event, name in [(process.stdin, selectors.EVENT_WRITE, "stdin"),
                    (process.stdout, selectors.EVENT_READ, "stdout"), (process.stderr, selectors.EVENT_READ, "stderr")]:
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                ready.register(stream, event, name)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(prepared.command, prepared.timeout)
                for key, _ in ready.select(min(remaining, .2)):
                    stream, channel = key.fileobj, key.data
                    if channel == "stdin":
                        if pending:
                            try:
                                count = os.write(stream.fileno(), pending[:65536])
                                pending = pending[count:]
                            except BrokenPipeError:
                                pending = pending[len(pending):]
                        if not pending:
                            ready.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        ready.unregister(stream)
                        stream.close()
                    if channel == "stderr":
                        if chunk:
                            yield "stderr", chunk.decode("utf-8", "replace")
                        continue
                    stdout_buffer += decoder.decode(chunk, final=not chunk)
                    while "\n" in stdout_buffer:
                        line, stdout_buffer = stdout_buffer.split("\n", 1)
                        yield "stdout", line + "\n"
                    if not chunk and stdout_buffer:
                        yield "stdout", stdout_buffer
                        stdout_buffer = ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(prepared.command, prepared.timeout)
            yield "exit", process.wait(timeout=remaining)
    finally:
        # Kill descendants too: they may still own inherited pipe descriptors.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _parse_stream_line(raw_line: str) -> Any | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"text": line}


def _stream_meta_payload(prepared: Any) -> dict[str, Any]:
    request = prepared.request
    return {
        "request_id": prepared.request_id,
        "provider": prepared.provider,
        "model": request.model,
        "cwd": str(prepared.cwd),
        "dry_run": request.dry_run,
        "conversation_id": prepared.conversation_id,
        "session_resumed": prepared.session_resumed,
        "command": shlex.join(prepared.command),
        "warnings": prepared.warnings,
    }


def _stream_done_payload(prepared: Any, text: str, started: float) -> dict[str, Any]:
    return {
        "request_id": prepared.request_id,
        "text": text,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "conversation_id": prepared.conversation_id,
        "session_resumed": prepared.session_resumed,
    }
