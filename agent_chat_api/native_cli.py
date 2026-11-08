"""Local, terminal-preserving supervision of the real provider CLIs.

The HTTP chat service does not import this entry point. A private launch profile
pins the real executables and the model choices for OMC's native workers.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import termios
import time
from typing import Any
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_chat_api.models import _sanitize_provider_error_text
from agent_chat_api.parsing import parse_provider_stdout
from agent_chat_api.provider_status import collect_provider_status


_MAX_LINE = 256 * 1024
_MAX_EVENTS = 128
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,159}\Z")
_NESTED_MARKERS = {"CHAT_API_KEY", "CLAUDECODE", "CLAUDE_SESSION_ID",
                   "CLAUDECODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"}
_UTILITIES = {"auth", "login", "logout", "update", "upgrade", "install", "doctor",
              "mcp", "plugin", "plugins", "completion", "completions"}
_VALUE_OPTIONS = {
    "claude": {"--agent", "--agents", "--allowedTools", "--allowed-tools", "--disallowedTools",
        "--disallowed-tools", "--append-system-prompt", "--append-system-prompt-file", "--system-prompt",
        "--system-prompt-file", "--betas", "--debug-file", "--effort", "--fallback-model", "--file",
        "--input-format", "--json-schema", "--max-budget-usd", "--max-turns", "--mcp-config",
        "--output-format", "--permission-mode", "--permission-prompt-tool", "--plugin-dir",
        "--session-id", "--settings", "--setting-sources", "--tools", "--name", "-n"},
    "codex": {"--config", "-c", "--enable", "--disable", "--image", "-i", "--local-provider",
        "--profile", "-p", "--sandbox", "-s", "--ask-for-approval", "-a", "--cd", "-C", "--add-dir",
        "--color", "--output-last-message", "-o", "--output-schema"},
    "opencode": {"--agent", "--server", "--session", "-s", "--format", "--file", "-f", "--title",
        "--prompt", "--log-level"},
}
# Model options exist only on these native subcommands.
_MODEL_SUBCOMMANDS = {"codex": {"exec", "e", "resume", "fork"}, "opencode": {"run"}}
# OpenCode keeps its own model when the user has never selected one.
_NATIVE_DEFAULT = "default"


class NativeCLIError(ValueError):
    """A deliberately non-sensitive, operator-facing validation failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _absolute(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise NativeCLIError(f"{label} must be an absolute path")
    return Path(value)


def _control_model(value: Any) -> bool:
    # User control inputs may include endpoint names, spaces, or '@'. Observed
    # telemetry uses the separate, deliberately stricter _MODEL expression.
    return (isinstance(value, str) and 0 < len(value) <= 1024 and bool(value.strip())
            and not any(character in value for character in "\0\r\n"))


def _owned_private(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)):
        raise NativeCLIError("launch profile and managed state must be private and owned by this user")


def load_run(path: Path, provider: str) -> dict[str, Any]:
    """Read only an owned, private profile; never find a provider through PATH."""
    _absolute(str(path), "run file")
    _owned_private(path.parent, directory=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise NativeCLIError("run file must be an owned regular file with mode 0600")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            data = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise NativeCLIError("unsupported launch profile")
    for key in ("run_id", "launch_id"):
        if not _uuid(data.get(key)):
            raise NativeCLIError("launch profile requires valid run and launch UUIDs")
    workspace = _absolute(data.get("workspace"), "workspace")
    provider_path = _absolute(data.get("provider_path"), "provider path")
    _absolute(data.get("state_dir"), "state directory")
    if not workspace.is_dir() or provider_path != path.parent:
        raise NativeCLIError("launch profile workspace or provider path is invalid")
    _owned_private(provider_path, directory=True)
    models, binaries = data.get("models"), data.get("native_binaries")
    if not isinstance(models, dict) or not isinstance(binaries, dict):
        raise NativeCLIError("launch profile requires provider models and native binaries")
    if not _control_model(models.get(provider)):
        raise NativeCLIError("launch profile contains an invalid provider model")
    binary = _absolute(binaries.get(provider), "native binary")
    resolved = binary.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise NativeCLIError("native provider binary is not executable")
    shims = {Path(__file__).resolve()}
    shims.update((provider_path / name).resolve() for name in _VALUE_OPTIONS)
    if resolved in shims or resolved.is_relative_to(provider_path.resolve()):
        raise NativeCLIError("native provider binary resolves to a wrapper shim")
    return data


def _private_dir(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        if not path.parent.exists():
            _private_dir(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    _owned_private(path, directory=True)


def _write_job(path: Path, record: dict[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _before_prompt(arguments: list[str]) -> list[str]:
    return arguments[:arguments.index("--")] if "--" in arguments else arguments


def _argument_groups(provider: str, arguments: list[str]) -> list[list[str]]:
    """Keep native option operands opaque, including operands starting with '-'."""
    groups: list[list[str]] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            groups.append(arguments[index:])
            break
        takes_value = value in _VALUE_OPTIONS[provider] | {"--model", "-m", "--wrapper-model"}
        if provider == "claude" and value in {"--resume", "-r"}:
            takes_value = index + 1 < len(arguments) and not arguments[index + 1].startswith("-")
        if provider == "claude" and value in {"--print", "-p"} and index + 1 < len(arguments):
            takes_value = not arguments[index + 1].startswith("-") or arguments[index + 1] in {"--help", "-h"}
        length = 2 if takes_value and index + 1 < len(arguments) and arguments[index + 1] != "--" else 1
        groups.append(arguments[index:index + length])
        index += length
    return groups


def _extract_models(groups: list[list[str]]) -> tuple[list[str], str | None, str | None]:
    cleaned: list[str] = []
    explicit = override = None
    for group in groups:
        argument = group[0]
        private = argument == "--wrapper-model" or argument.startswith("--wrapper-model=")
        native = argument in {"--model", "-m"} or argument.startswith("--model=")
        if private or native:
            if "=" in argument:
                value = argument.split("=", 1)[1]
            else:
                if len(group) != 2:
                    raise NativeCLIError("a model option is missing its value")
                value = group[1]
            if not _control_model(value):
                raise NativeCLIError("invalid model identifier")
            if private:
                override = value
            else:
                explicit = value
        else:
            cleaned.extend(group)
    return cleaned, explicit, override


def prepare_arguments(provider: str, arguments: list[str], run: dict[str, Any],
                      environment: dict[str, str]) -> tuple[list[str], str | None, bool]:
    groups = _argument_groups(provider, arguments)
    options = [group for group in groups if group[0] != "--"]
    utility = any(group[0] in {"--help", "-h", "--version", "-v"} for group in options)
    first_command = next((group[0] for group in options if not group[0].startswith("-")), None)
    utility |= first_command in _UTILITIES
    # The TUI and server commands have no model option; they keep OpenCode's own selection.
    utility |= provider == "opencode" and first_command != "run"
    if utility:
        # Even utilities may carry an explicit native model option. Leave those
        # options exactly as supplied and remove only our private override.
        utility_args: list[str] = []
        for group in groups:
            if group[0] != "--wrapper-model" and not group[0].startswith("--wrapper-model="):
                utility_args.extend(group)
        return utility_args, None, True
    cleaned, explicit, override = _extract_models(groups)
    selected = override or (run["models"][provider] if environment.get("OMC_TEAM_WORKER") else explicit)
    selected = selected or run["models"][provider]
    if provider == "claude":
        cleaned = [item for group in _argument_groups(provider, cleaned)
                   for item in (["--dangerously-skip-permissions"] if group == ["--yolo"] else group)]
    if selected == _NATIVE_DEFAULT:
        return cleaned, None, False
    # A subcommand has its own model option scope.
    position = 0
    if provider in _MODEL_SUBCOMMANDS:
        for group in _argument_groups(provider, cleaned):
            if group[0] == "--":
                break
            position += len(group)
            if not group[0].startswith("-"):
                if group[0] not in _MODEL_SUBCOMMANDS[provider]:
                    position = 0
                break
        else:
            position = 0
    return [*cleaned[:position], "--model", selected, *cleaned[position:]], selected, False


def child_environment(run: dict[str, Any], run_file: Path, job_id: str | None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in _NESTED_MARKERS}
    environment["PATH"] = run["provider_path"] + os.pathsep + environment.get("PATH", os.defpath)
    environment.update({"OMC_WRAPPER_RUN_ID": run["run_id"], "OMC_WRAPPER_RUN_FILE": str(run_file),
                        "OMC_WRAPPER_STATE_DIR": run["state_dir"]})
    if job_id is not None:
        environment["OMC_WRAPPER_JOB_ID"] = job_id
    return environment


class Redactor:
    def __init__(self, environment: dict[str, str], arguments: list[str], model: str | None):
        self.secrets = sorted({value for key, value in environment.items()
                               if re.search(r"key|token|password|secret|credential|authorization", key, re.I)
                               and len(value) >= 4}, key=len, reverse=True)
        self.prompts = [value for value in arguments if len(value) >= 8
                        and not value.startswith("-") and value != model]

    def text(self, value: str, *, evidence: bool = False) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        if evidence:
            for prompt in self.prompts:
                value = value.replace(prompt, "[prompt redacted]")
        return _sanitize_provider_error_text(value)

    def record(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.text(item, evidence=True) if key == "message" and isinstance(item, str)
                    else self.record(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.record(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _model(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _text(value: Any, limit: int = 4096) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _error(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("error")
    information = event.get("codex_error_info")
    if isinstance(information, dict):
        information = next(iter(information), None)
    known = {"cyber_policy", "bio_policy", "misalignment_policy_violation"}
    if isinstance(information, str) and information in known:
        return {"type": "error", "error": {"code": information,
                "message": _text(event.get("message"), 1024)}, "_native_error_code": information}
    if information == "policy_capacity_unavailable":
        return {"type": "error", "code": information, "message": "Provider policy capacity is unavailable.",
                "_native_error_code": information}
    if isinstance(value, dict):
        return {"type": "error", "error": {"code": _text(value.get("code") or value.get("type"), 160),
                "message": _text(value.get("message"), 1024)}}
    errors = event.get("errors")
    message = value or event.get("message") or event.get("result")
    if not isinstance(message, str) and isinstance(errors, list):
        message = "; ".join(_text(item, 320) for item in errors[:5])
    return {"type": "error", "code": _text(event.get("code") or event.get("subtype"), 160),
            "message": _text(message, 1024)}


def normalize_event(provider: str, event: Any) -> list[dict[str, Any]]:
    """Retain provider observations, excluding prompts, tools and reasoning."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if provider == "codex" and kind == "turn_context":
        payload = event.get("payload")
        if isinstance(payload, dict) and _model(payload.get("model")):
            return [{"type": "system", "subtype": "init", "model": payload["model"]}]
        return []
    if provider == "codex" and kind == "event_msg":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return []
        subtype = payload.get("type")
        if subtype in {"model_reroute", "model_rerouted"}:
            origin, target = _model(payload.get("from_model")), _model(payload.get("to_model"))
            reason = _text(payload.get("reason"), 160)
            canonical = "HighRiskCyberActivity" if reason == "high_risk_cyber_activity" else reason
            if origin and target:
                return [{"type": "item.completed", "item": {"type": "error",
                         "message": f"model rerouted: {origin} -> {target} ({canonical or 'not_exposed'})"},
                         "_native_reason": reason}]
            return [{"type": "model_rerouted", "from_model": origin, "to_model": target,
                     "reason": canonical, "_native_reason": reason}]
        if subtype == "agent_message":
            result = [{"type": "item.completed", "item": {"type": "agent_message",
                       "text": _text(payload.get("message"))}}]
            if _model(payload.get("model")):
                result.append({"type": "result", "model": payload["model"]})
            return result
        if subtype == "task_complete":
            answer = _text(payload.get("last_agent_message"))
            result = [{"type": "item.completed", "item": {"type": "agent_message", "text": answer}}] if answer else []
            if _model(payload.get("model")):
                result.append({"type": "result", "model": payload["model"]})
            return [*result, {"type": "turn.completed"}]
        if subtype == "error":
            return [_error(payload)]
        return []
    if provider == "opencode":
        part = event.get("part")
        if kind == "text" and isinstance(part, dict):
            return [{"type": "text", "part": {"text": _text(part.get("text")), "messageID": part.get("messageID")}}]
        failure = event.get("error")
        if kind == "error" and isinstance(failure, dict):
            return [{"type": "error", "error": {"type": _text(failure.get("type"), 160),
                                                "message": _text(failure.get("message"))}}]
        return []
    if kind == "assistant" and provider == "claude":
        message = event.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        blocks = []
        remaining = 4096
        for block in content[:32] if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "refusal":
                blocks.append({"type": "refusal"})
            elif block.get("type") == "text" and remaining:
                text = _text(block.get("text"), remaining)
                blocks.append({"type": "text", "text": text})
                remaining -= len(text)
        details = message.get("stop_details")
        return [{"type": "assistant", "message": {"model": _model(message.get("model")),
                 "stop_reason": _text(message.get("stop_reason"), 160),
                 "stop_details": {"category": _text(details.get("category"), 160)} if isinstance(details, dict) else {},
                 "content": blocks}}]
    if kind == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            return [{"type": "system", "subtype": "init", "model": _model(event.get("model"))}]
        if subtype in {"model_reroute", "model_rerouted", "model_fallback"}:
            return [{"type": "system", "subtype": subtype, "from_model": _model(event.get("from_model")),
                     "to_model": _model(event.get("to_model")), "reason": _text(event.get("reason"), 320)}]
        if subtype in {"api_error", "error"}:
            return [_error(event)]
        return []
    if kind in {"error", "turn.failed"}:
        return [_error(event)]
    if kind in {"result", "response.completed"}:
        result = {"type": kind, "model": _model(event.get("model")), "result": _text(event.get("result")),
                  "is_error": event.get("is_error") is True, "subtype": _text(event.get("subtype"), 160)}
        usage = event.get("modelUsage")
        if isinstance(usage, dict):
            result["modelUsage"] = {name: {"outputTokens": counts["outputTokens"]}
                                    for name, counts in list(usage.items())[:32] if _model(name)
                                    and isinstance(counts, dict) and isinstance(counts.get("outputTokens"), (int, float))}
        if result["is_error"] or result["subtype"].startswith("error_"):
            return [*([result] if result["model"] else []), _error(event)]
        return [result]
    if kind == "item.completed" and provider == "codex":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return [{"type": kind, "item": {"type": "agent_message", "text": _text(item.get("text"))}}]
        if isinstance(item, dict) and item.get("type") == "error":
            return [{"type": kind, "item": {"type": "error", "message": _text(item.get("message"), 1024)}}]
    if kind == "turn.completed":
        return [{"type": kind}]
    if kind in {"model_reroute", "model_rerouted", "model_fallback"}:
        return [{"type": kind, "from_model": _model(event.get("from_model")),
                 "to_model": _model(event.get("to_model")), "reason": _text(event.get("reason"), 320)}]
    return []


class Observations:
    def __init__(self, provider: str):
        self.provider = provider
        self.events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self.sources: set[str] = set()
        self.stderr = bytearray()
        self.final_answer = bytearray()

    def add(self, value: Any, source: str) -> None:
        for event in value if isinstance(value, list) else [value]:
            normalized = normalize_event(self.provider, event)
            if normalized:
                self.events.extend(normalized)
                self.sources.add(source)

    def status(self, requested: str, exit_code: int | None,
               other: Observations | None = None) -> dict[str, Any]:
        events = [*self.events, *(other.events if other else [])]
        text, raw = parse_provider_stdout(self.provider, json.dumps(events)) if events else ("", None)
        if not events and exit_code is not None and self.final_answer:
            try:
                answer = self.final_answer.decode("utf-8")
                if "\0" not in answer:
                    text = answer
                    self.sources.add("native_final_answer")
            except UnicodeDecodeError:
                pass
        status = collect_provider_status(provider=self.provider, requested_model=requested,
                                         raw_json=raw, text=text,
                                         stderr=self.stderr.decode("utf-8", "replace"), exit_code=exit_code or 0)
        diagnostics = any(item["source"] == "provider_diagnostic" for item in status["signals"])
        if diagnostics:
            self.sources.add("native_stderr")
        if not events and not diagnostics and not text.strip():
            status["block"]["detected"] = None
            status["outcome"] = "unknown" if not exit_code else "failed"
            status["reason_code"] = None if not exit_code else "provider_error"
        elif status["reason_code"] == "provider_empty_response":
            status["outcome"] = "unknown" if not exit_code else "failed"
            status["reason_code"] = None if not exit_code else "provider_error"
        if exit_code is None:
            status["outcome"] = "running"
        for entry in status["signals"] + status["safety_flags"]:
            if entry["source"] == "response_text":
                entry["message"] = "Provider response text matched a refusal notice."
        native_reasons = [event["_native_reason"] for event in events if "_native_reason" in event]
        for entry, reason in zip(status["model_switches"], native_reasons):
            entry["provider_reason"] = reason
        native_codes = [event["_native_error_code"] for event in events if "_native_error_code" in event]
        for entry, code in zip((item for item in status["signals"] if item["kind"] == "error"), native_codes):
            entry["provider_code"] = code
        return status


class Lines:
    """A bounded decoder: an oversized line is skipped, never partly parsed."""
    def __init__(self, observations: Observations, source: str):
        self.observations, self.source = observations, source
        self.buffer = bytearray()
        self.dropping = False

    def feed(self, data: bytes, *, final: bool = False) -> None:
        pieces = data.split(b"\n")
        for index, piece in enumerate(pieces):
            if not self.dropping:
                if len(self.buffer) + len(piece) > _MAX_LINE:
                    self.buffer.clear()
                    self.dropping = True
                else:
                    self.buffer.extend(piece)
            if index < len(pieces) - 1 or final:
                if not self.dropping and self.buffer:
                    try:
                        self.observations.add(json.loads(self.buffer), self.source)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        pass
                self.buffer.clear()
                self.dropping = False


def _process_tree(pid: int) -> set[int]:
    result: set[int] = set()
    pending = [pid]
    while pending and len(result) < 512:
        current = pending.pop()
        if current in result or current <= 1 or current == os.getpid():
            continue
        result.add(current)
        try:
            pending.extend(int(value) for value in Path(f"/proc/{current}/task/{current}/children").read_text().split())
        except (OSError, ValueError):
            pass
    return result


def _process_identity(pid: int) -> str | None:
    try:
        # comm may contain spaces or parentheses. Fields after its last ')' start at state.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


class Trace:
    def __init__(self, provider: str, arguments: list[str], environment: dict[str, str], cwd: str,
                 *, create_claude_session: bool = True):
        self.provider, self.cwd = provider, cwd
        self.observations = Observations(provider)
        self.lines = Lines(self.observations, provider + ("_transcript" if provider == "claude" else "_rollout"))
        self.path: Path | None = None
        self.stream: Any = None
        self.offset = 0
        self.ambiguous = False
        self.codex_home = Path(environment.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
        self.resuming = "resume" in _before_prompt(arguments) or "fork" in _before_prompt(arguments)
        if provider == "claude":
            self.path = self._claude_path(arguments, environment, create_claude_session)
            if self.path:
                try:
                    self.offset = self.path.stat().st_size
                except OSError:
                    pass

    def _claude_path(self, arguments: list[str], environment: dict[str, str], create_session: bool) -> Path | None:
        groups = [group for group in _argument_groups("claude", arguments) if group[0] != "--"]
        head = [group[0] for group in groups]
        if any(value in {"--no-session-persistence", "--fork-session"} for value in head):
            return None
        session = None
        explicit_session = False
        for group in groups:
            value = group[0]
            if value in {"--session-id", "--resume", "-r"}:
                explicit_session = True
                if len(group) == 2:
                    session = _uuid(group[1])
            elif value.startswith(("--session-id=", "--resume=")):
                explicit_session, session = True, _uuid(value.split("=", 1)[1])
            elif value in {"--continue", "-c"}:
                explicit_session, session = True, None
        if not explicit_session and create_session:
            session = str(uuid.uuid4())
            arguments[0:0] = ["--session-id", session]
        if not session:
            return None
        root = Path(environment.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
        encoded = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        return root / "projects" / encoded / f"{session}.jsonl"

    def _open(self, path: Path, offset: int) -> Any:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                return None
            stream = os.fdopen(descriptor, "rb", buffering=0)
            descriptor = -1
            stream.seek(offset)
            return stream
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _codex_candidates(self, pid: int) -> set[Path]:
        candidates: set[Path] = set()
        for child in _process_tree(pid):
            try:
                descriptors = list(Path(f"/proc/{child}/fd").iterdir())
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    path = Path(os.readlink(descriptor))
                    if not path.name.startswith("rollout-") or path.suffix != ".jsonl":
                        continue
                    resolved = path.resolve(strict=True)
                    if any(resolved.is_relative_to(self.codex_home / folder) for folder in ("sessions", "archived_sessions")):
                        candidates.add(resolved)
                except OSError:
                    continue
        verified: set[Path] = set()
        for path in candidates:
            try:
                stream = self._open(path, 0)
                if stream is None:
                    continue
                with stream:
                    line = stream.readline(_MAX_LINE + 1)
                event = json.loads(line) if len(line) <= _MAX_LINE else None
                payload = event.get("payload") if isinstance(event, dict) else None
                if (isinstance(event, dict) and event.get("type") == "session_meta"
                        and isinstance(payload, dict) and payload.get("cwd") == self.cwd):
                    verified.add(path)
            except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                continue
        return verified

    def poll(self, pid: int) -> None:
        if self.ambiguous:
            return
        if self.provider == "codex":
            candidates = self._codex_candidates(pid)
            if self.path:
                candidates.add(self.path)
            if len(candidates) > 1:
                self.ambiguous = True
                self.observations.events.clear()
                self.observations.sources = {"codex_rollout_ambiguous"}
                self.close()
                return
            if candidates and not self.path:
                self.path = candidates.pop()
                self.offset = self.path.stat().st_size if self.resuming else 0
        if self.path is None:
            return
        try:
            if self.stream is None:
                self.stream = self._open(self.path, self.offset)
            if self.stream is not None:
                self.observations.sources.add(self.lines.source)
                for _ in range(8):
                    data = self.stream.read(65536)
                    if not data:
                        break
                    self.lines.feed(data)
        except OSError:
            pass

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class Signals:
    """Wait for signals synchronously so terminal Ctrl-C is not sent twice."""
    signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}

    def __enter__(self) -> Signals:
        self.mask = signal.pthread_sigmask(signal.SIG_BLOCK, self.signals)
        return self

    def child_setup(self) -> None:
        signal.pthread_sigmask(signal.SIG_SETMASK, self.mask)

    def pending(self) -> list[Any]:
        result = []
        while (info := signal.sigtimedwait(self.signals, 0)) is not None:
            result.append(info)
        return result

    def __exit__(self, *_: Any) -> None:
        self.pending()
        signal.pthread_sigmask(signal.SIG_SETMASK, self.mask)


def _relay_output(descriptor: int, data: bytes) -> None:
    while data:
        try:
            written = os.write(descriptor, data)
            data = data[written:]
        except (BrokenPipeError, OSError):
            return


def supervise(provider: str, arguments: list[str], run_file: Path, run: dict[str, Any],
              requested: str) -> int:
    job_id = str(uuid.uuid4())
    environment = child_environment(run, run_file, job_id)
    if provider == "opencode":
        environment.setdefault("OPENCODE_NO_AUTOLLAMA", "1")
    cwd, started = os.getcwd(), _now()
    interactive = os.isatty(0) and os.isatty(1)
    saved_terminal = termios.tcgetattr(0) if interactive else None
    groups = [group for group in _argument_groups(provider, arguments) if group[0] != "--"]
    head = [group[0] for group in groups]
    printing = provider == "claude" and any(value in {"--print", "-p"} for value in head)
    trace = Trace(provider, arguments, environment, cwd, create_claude_session=interactive or printing)
    output_format = next((group[1] for group in groups if group[0] == "--output-format" and len(group) == 2), None)
    output_format = next((value.split("=", 1)[1] for value in head if value.startswith("--output-format=")), output_format)
    plain_answer = ((printing and output_format in {None, "text"}) or
                    (provider == "codex" and next((value for value in head if not value.startswith("-")), None) in {"exec", "e"}
                     and "--json" not in head))
    observations = Observations(provider)
    lines = Lines(observations, "native_stdout")
    redactor = Redactor(dict(os.environ), arguments, requested)
    record: dict[str, Any] = {"schema_version": 1, "job_id": job_id, "run_id": run["run_id"],
        "launch_id": run["launch_id"], "provider": provider, "kind": "agent", "owner_pid": os.getpid(),
        "provider_pid": None, "parent_job_id": _uuid(os.environ.get("OMC_WRAPPER_JOB_ID")),
        "cwd": cwd, "requested_model": requested, "started_at": started, "updated_at": started,
        "job_state": "running", "exit_code": None, "provider_status": observations.status(requested, None),
        "telemetry_sources": []}
    for name, key in (("worker", "OMC_TEAM_WORKER"), ("role", "OMC_TEAM_ROLE")):
        if environment.get(key):
            record[name] = environment[key][:160]
    state = Path(run["state_dir"])
    for directory in (state, state / "runs", state / "runs" / run["run_id"], state / "runs" / run["run_id"] / "jobs"):
        _private_dir(directory)
    job_path = state / "runs" / run["run_id"] / "jobs" / f"{job_id}.json"
    _write_job(job_path, redactor.record(record))  # Fail closed before any provider starts.
    process: subprocess.Popen[bytes] | None = None
    exit_code = 127
    cancellation: dict[int, str] = {}
    observed_children: dict[int, str] = {}
    kill_deadline: float | None = None
    killed_at: float | None = None

    def cancel(signum: int) -> None:
        nonlocal kill_deadline
        if process is None:
            return
        for pid in _process_tree(process.pid) | observed_children.keys():
            identity = _process_identity(pid)
            if pid in observed_children and identity != observed_children[pid]:
                continue
            if identity:
                cancellation[pid] = identity
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass
        kill_deadline = time.monotonic() + 3

    def update(code: int | None) -> None:
        record["updated_at"] = _now()
        record["provider_status"] = observations.status(requested, code, trace.observations if trace else None)
        record["telemetry_sources"] = sorted(observations.sources | (trace.observations.sources if trace else set()))
        _write_job(job_path, redactor.record(record))

    try:
        with Signals() as signals:
            try:
                process = subprocess.Popen([run["native_binaries"][provider], *arguments], stdin=None,
                    stdout=None if interactive else subprocess.PIPE, stderr=None if interactive else subprocess.PIPE,
                    cwd=None, env=environment, preexec_fn=signals.child_setup)
                record["provider_pid"] = process.pid
                update(None)
                with selectors.DefaultSelector() as selector:
                    if not interactive:
                        for stream, descriptor in ((process.stdout, 1), (process.stderr, 2)):
                            if stream is not None:
                                os.set_blocking(stream.fileno(), False)
                                selector.register(stream, selectors.EVENT_READ, descriptor)
                    last_write = time.monotonic()
                    while (process.poll() is None or selector.get_map() or any(
                            _process_identity(pid) == identity for pid, identity in cancellation.items())):
                        for pid in _process_tree(process.pid):
                            identity = _process_identity(pid)
                            if identity:
                                observed_children[pid] = identity
                        for info in signals.pending():
                            terminal_interrupt = (interactive and info.si_signo == signal.SIGINT
                                                  and info.si_code == getattr(signal, "SI_KERNEL", 128))
                            if not terminal_interrupt:
                                cancel(info.si_signo)
                        if kill_deadline is not None and time.monotonic() >= kill_deadline:
                            for pid, identity in cancellation.items():
                                if _process_identity(pid) == identity:
                                    try:
                                        os.kill(pid, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                            kill_deadline = None
                            killed_at = time.monotonic()
                        if killed_at is not None and time.monotonic() - killed_at >= 1 and process.poll() is not None:
                            break
                        for key, _ in selector.select(0.05):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            else:
                                _relay_output(key.data, data)
                                if key.data == 1:
                                    lines.feed(data)
                                    if plain_answer and len(observations.final_answer) < 8192:
                                        observations.final_answer.extend(data[:8192 - len(observations.final_answer)])
                                else:
                                    observations.stderr.extend(data)
                                    del observations.stderr[:-16384]
                        if trace:
                            trace.poll(process.pid)
                        if time.monotonic() - last_write >= 0.5:
                            update(None)
                            last_write = time.monotonic()
                    exit_code = process.wait()
                    if exit_code < 0:
                        exit_code = 128 - exit_code
            except BaseException:
                cancel(signal.SIGTERM)
                if process is not None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        cancel(signal.SIGKILL)
                        process.wait()
                raise
    except (OSError, subprocess.SubprocessError):
        _relay_output(2, b"omc provider wrapper: native process or private status update failed\n")
        exit_code = 127 if process is None else 1
    finally:
        if process is not None and trace is not None:
            trace.poll(process.pid)
        lines.feed(b"", final=True)
        if trace:
            trace.lines.feed(b"", final=True)
            trace.close()
        if saved_terminal is not None:
            try:
                termios.tcsetattr(0, termios.TCSANOW, saved_terminal)
            except termios.error:
                pass
        record["job_state"] = "exited" if exit_code == 0 else "failed"
        record["exit_code"] = exit_code
        update(exit_code)
    if not interactive:
        summary = {"job_id": job_id, "provider": provider, **record["provider_status"]}
        _relay_output(2, b"\n[omc-provider-status] " + json.dumps(redactor.record(summary), separators=(",", ":")).encode() + b"\n")
    return exit_code


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise a native provider from an owned OMC launch profile.")
    parser.add_argument("--provider", choices=tuple(_VALUE_OPTIONS), required=True)
    parser.add_argument("--run-file", type=Path, required=True)
    parser.add_argument("native_arguments", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    native = parsed.native_arguments
    if native[:1] == ["--"]:
        native = native[1:]
    try:
        run = load_run(parsed.run_file, parsed.provider)
        command, requested, utility = prepare_arguments(parsed.provider, native, run, dict(os.environ))
        if utility:
            os.execve(run["native_binaries"][parsed.provider], [run["native_binaries"][parsed.provider], *command],
                      child_environment(run, parsed.run_file, None))
        return supervise(parsed.provider, command, parsed.run_file, run, requested or "")
    except NativeCLIError as error:
        _relay_output(2, f"omc provider wrapper: {error}\n".encode())
        return 2
    except (OSError, ValueError, UnicodeDecodeError):
        _relay_output(2, b"omc provider wrapper: cannot read launch profile or write private job status\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
