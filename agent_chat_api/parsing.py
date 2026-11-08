# =========================================
# File: agent_chat_api/parsing.py
# Purpose:
#   - Converts provider stdout/stderr and stream payload fragments into plain
#     text, raw JSON, and resumable session identifiers.
# What This File Does:
#   - Understands Claude JSON output shapes such as `result`, `completion`,
#     `text`, `message`, and list-style `content` blocks.
#   - Pulls Codex session ids out of stderr so conversation persistence can
#     resume the right provider session later.
#   - Extracts text deltas from stream events whether the text is top-level,
#     nested under `delta`, wrapped in a `message`, or stored in content blocks.
# Main Functions:
#   - `parse_claude_stdout()`: Returns the best readable text plus raw decoded
#     JSON when Claude returns structured output, falling back to raw stdout.
#   - `parse_codex_session_id()`: Finds the provider session id emitted by Codex.
#   - `extract_stream_text()` / `content_to_text()`: Normalize heterogeneous
#     stream payloads into text chunks for SSE deltas.
#   - `optional_string()`: Converts optional payload values into stripped text
#     or `None`.
# Used By:
#   - `agent_chat_api.providers` uses these helpers after provider subprocesses
#     finish.
#   - `agent_chat_api.streaming` uses the stream-text helpers while forwarding
#     provider events.
# Reader Note:
#   - Keep parser changes defensive: provider CLIs change output shape over time,
#     so this module deliberately accepts several equivalent text containers.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import json
import re
from typing import Any


def parse_claude_stdout(stdout: str) -> tuple[str, dict[str, Any] | list[Any] | None]:
    stripped = stdout.strip()
    if not stripped:
        return "", None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped, None
    if isinstance(data, dict):
        for key in ("result", "completion", "text", "message"):
            value = data.get(key)
            if isinstance(value, str):
                return value.strip(), data
        content = data.get("content")
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
            if text:
                return text.strip(), data
    return stripped, data


def parse_provider_stdout(provider: str, stdout: str) -> tuple[str, Any]:
    """Separate answers from CLI lifecycle, tool, and error events."""
    stripped = stdout.strip()
    if not stripped:
        return "", None
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            decoded = [json.loads(line) for line in stripped.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return stripped, None
    events = decoded if isinstance(decoded, list) else [decoded]
    if not all(isinstance(event, dict) for event in events):
        return stripped, decoded
    answers: list[str] = []
    final: str | None = None
    recognized = False
    opencode_message: Any = None
    for event in events:
        kind = event.get("type")
        recognized |= isinstance(kind, str)
        if provider == "codex" and kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                answers.append(item["text"])
        elif provider == "opencode" and kind == "text":
            part = event.get("part") or {}
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                # Only the last assistant message is the answer; earlier ones precede tool calls.
                if part.get("messageID") != opencode_message:
                    answers.clear()
                    opencode_message = part.get("messageID")
                answers.append(part["text"])
        elif provider == "claude":
            if kind == "result" and isinstance(event.get("result"), str):
                final = event["result"]
            elif kind == "assistant" and isinstance(event.get("message"), dict):
                text = content_to_text(event["message"].get("content"))
                if text:
                    answers.append(text)
    if provider == "opencode" and answers:
        final = "".join(answers)
    if final is not None or answers:
        return (final if final is not None else answers[-1]).strip(), decoded
    if not recognized and provider == "claude":
        return parse_claude_stdout(stdout)
    return ("" if recognized else stripped), decoded


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_codex_session_id(stderr: str) -> str | None:
    match = re.search(r"session id:\s*([A-Za-z0-9._~+/\-=:-]+)", stderr, flags=re.IGNORECASE)
    return match.group(1) if match else None


def extract_stream_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("text"), str):
        return str(value["text"])
    part = value.get("part")
    if value.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
        return str(part["text"])
    delta = value.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return str(delta["text"])
    message = value.get("message")
    if isinstance(message, dict):
        text = content_to_text(message.get("content"))
        if text:
            return text
    return content_to_text(value.get("content"))


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
        return "".join(parts)
    return ""
