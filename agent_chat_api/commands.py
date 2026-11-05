# =========================================
# File: agent_chat_api/commands.py
# Purpose:
#   - Converts validated chat requests into the exact command-line invocations
#     used to launch Claude or Codex providers.
# What This File Does:
#   - Builds Claude CLI commands with stream/non-stream output formats,
#     optional session resume/persistence, tool permissions, model, and effort.
#   - Builds Codex CLI commands for fresh execution and session resume, including
#     sandbox mode, working directory, JSON streaming, model, effort, and
#     ephemeral behavior.
#   - Enforces the local safety rule that `danger-full-access` requires an
#     explicit settings flag.
# Main Functions:
#   - `build_claude_command()`: Returns the Claude command argv plus warnings
#     for unsupported wrapper options such as multi-turn requests.
#   - `build_codex_command()`: Validates the requested Codex sandbox and returns
#     either a resume command or a fresh `codex exec` command.
# Used By:
#   - `agent_chat_api.providers` uses these builders before spawning provider
#     subprocesses.
#   - Provider tests exercise the generated argv to keep wrapper behavior stable.
# Reader Note:
#   - This file should only describe commands. Request validation belongs in
#     payload preparation, while process execution and stream parsing belong in
#     provider/streaming modules.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .models import ChatRequest, RequestError


CODEX_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}


def build_claude_command(
    settings: Settings,
    request: ChatRequest,
    *,
    stream: bool,
    should_persist: bool,
    provider_session_id: str | None,
    session_resumed: bool,
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    # Capture lifecycle and assistant model observations even for a buffered HTTP request.
    output_format = "stream-json"
    command = [
        settings.claude_bin,
        "-p",
        "--output-format",
        output_format,
        "--verbose",
        "--input-format",
        "text",
        "--disable-slash-commands",
    ]
    if stream:
        command.append("--include-partial-messages")
    if session_resumed and provider_session_id:
        command.extend(["--resume", provider_session_id])
    elif should_persist and provider_session_id:
        command.extend(["--session-id", provider_session_id])
    else:
        command.append("--no-session-persistence")
    if request.claude_max_turns != 1:
        warnings.append("claude_max_turns is ignored by the CLI wrapper; use the Claude Agent SDK for multi-turn clients.")
    if request.allow_tools:
        tools = request.claude_tools or "default"
        command.extend(["--tools", tools, "--permission-mode", request.claude_permission_mode or "default"])
    else:
        command.extend(["--tools", "", "--permission-mode", "dontAsk"])
    if request.model:
        command.extend(["--model", request.model])
    if request.effort:
        command.extend(["--effort", request.effort])
    return command, warnings


def build_codex_command(
    settings: Settings,
    request: ChatRequest,
    cwd: Path,
    *,
    stream: bool,
    should_persist: bool,
    provider_session_id: str | None,
    session_resumed: bool,
) -> tuple[list[str], list[str]]:
    sandbox = request.codex_sandbox
    if sandbox not in CODEX_SANDBOXES:
        raise RequestError(f"codex_sandbox must be one of: {', '.join(sorted(CODEX_SANDBOXES))}")
    if sandbox == "danger-full-access" and not settings.allow_dangerous_sandboxes:
        raise RequestError("danger-full-access is disabled. Set CHAT_API_ALLOW_DANGEROUS_SANDBOXES=1 to allow it.")

    warnings: list[str] = []
    if not request.allow_tools:
        warnings.append("Codex CLI does not expose a hard no-tools flag here; read-only sandbox and guard prompt are used.")

    if session_resumed and provider_session_id:
        command = [settings.codex_bin, "exec", "resume", "--skip-git-repo-check"]
        command.append("--json")
        if request.model:
            command.extend(["--model", request.model])
        if request.effort:
            command.extend(["-c", f'model_reasoning_effort="{request.effort}"'])
        command.extend([provider_session_id, "-"])
        return command, warnings

    command = [
        settings.codex_bin,
        "exec",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "--cd",
        str(cwd),
        "--skip-git-repo-check",
    ]
    command.append("--json")
    if request.codex_ephemeral and not should_persist:
        command.append("--ephemeral")
    if request.model:
        command.extend(["--model", request.model])
    if request.effort:
        command.extend(["-c", f'model_reasoning_effort="{request.effort}"'])
    command.append("-")
    return command, warnings
