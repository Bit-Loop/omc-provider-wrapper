"""Real subprocess regression checks for timeout, pipe pressure and cancellation."""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from agent_chat_api.streaming import stream_events
from agent_chat_api.payloads import request_from_payload
from agent_chat_api.models import RequestError


def stream(tmp_path, code, *, timeout=2, prompt="fixture"):
    prepared = SimpleNamespace(command=[sys.executable, "-c", code], cwd=tmp_path,
        provider="claude", prompt=prompt, timeout=timeout, request_id="fixture",
        conversation_id=None, session_resumed=False, warnings=[],
        request=SimpleNamespace(dry_run=False, model=None))
    return stream_events(prepared, record_session=lambda *args, **kwargs: None,
        redact_json_value=lambda value: value, redact_text=lambda value: value,
        extract_stream_text=lambda value: value.get("result", "") if isinstance(value, dict) else "")


def assert_stopped(pid_file):
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_stream_drains_stderr_while_feeding_large_stdin(tmp_path):
    code = "import sys,json; sys.stderr.write('x'*1048576); sys.stderr.flush(); sys.stdin.read(); print(json.dumps({'type':'result','result':'pipe-pressure-ok','is_error':False}))"
    events = list(stream(tmp_path, code, prompt="p" * 200_000))
    assert events[-1].event == "done"
    assert events[-1].data["text"] == "pipe-pressure-ok"


def test_stream_deadline_terminates_and_reaps_provider(tmp_path):
    pid_file = tmp_path / "provider.pid"
    code = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    started = time.monotonic()
    events = list(stream(tmp_path, code, timeout=.3))
    assert time.monotonic() - started < 3
    assert events[-1].event == "error"
    assert events[-1].data["code"] == "provider_timeout"
    assert_stopped(pid_file)


def test_closing_stream_cancels_native_child(tmp_path):
    pid_file = tmp_path / "provider.pid"
    code = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); print('{{\"type\":\"system\"}}', flush=True); time.sleep(30)"
    events = stream(tmp_path, code)
    assert next(events).event == "meta"
    assert next(events).event == "provider_event"
    events.close()
    assert_stopped(pid_file)


@pytest.mark.parametrize("messages", [["malformed"], [None], [42]])
def test_malformed_message_elements_are_request_errors(messages):
    with pytest.raises(RequestError, match="must be an object"):
        request_from_payload({"messages": messages}, "codex")
