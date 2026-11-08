"""Exercise the public local frontend with owned fake native executables."""
from __future__ import annotations

import base64
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import uuid

import pytest

from agent_chat_api.native_cli import normalize_event, prepare_arguments


CLI = Path(__file__).resolve().parents[1] / "native_cli.py"
FAKE = r'''
import base64, json, os, pathlib, signal, subprocess, sys, termios, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "batch")
capture = pathlib.Path(os.environ["FAKE_CAPTURE"])
details = {"args": args, "cwd": os.getcwd(), "tty": [os.isatty(fd) for fd in (0, 1, 2)],
           "pgid": os.getpgrp(), "sid": os.getsid(0), "ppid": os.getppid(),
           "env": {key: value for key, value in os.environ.items()
                   if key.startswith(("OMC_", "CLAUDE", "CHAT_")) or key in
                   {"PATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ORDINARY_PROVIDER_OPTION"}}}
if mode == "batch":
    details["stdin"] = sys.stdin.buffer.read().decode("utf-8", "replace")
capture.write_text(json.dumps(details))
if mode == "tty":
    details["foreground"] = os.tcgetpgrp(0) == os.getpgrp()
    capture.write_text(json.dumps(details))
    attrs = termios.tcgetattr(0)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(0, termios.TCSANOW, attrs)
elif mode == "claude_trace":
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else os.environ["FAKE_SESSION"]
    encoded = "".join(c if c.isascii() and c.isalnum() else "-" for c in os.getcwd())
    path = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / encoded / (sid + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for event in json.loads(os.environ["FAKE_EVENTS"]):
            stream.write(json.dumps(event) + "\n")
        stream.flush()
        time.sleep(.25)
elif mode.startswith("codex_trace"):
    home = pathlib.Path(os.environ["CODEX_HOME"])
    folder = home / "sessions" if mode != "codex_trace_outside" else home.parent / "elsewhere"
    folder.mkdir(parents=True, exist_ok=True)
    streams = []
    for number in range(2 if mode == "codex_trace_ambiguous" else 1):
        path = folder / ("rollout-fixture-" + str(number) + ".jsonl")
        stream = path.open("w")
        streams.append(stream)
        stream.write(json.dumps({"type": "session_meta", "payload": {
            "cwd": os.getcwd() if mode != "codex_trace_wrong_cwd" else "/other/workspace"}}) + "\n")
        for event in json.loads(os.environ["FAKE_EVENTS"]):
            stream.write(json.dumps(event) + "\n")
        stream.flush()
    time.sleep(.3)
    for stream in streams:
        stream.close()
elif mode == "signal":
    if os.environ.get("FAKE_DESCENDANT"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        details["descendant"] = child.pid
        capture.write_text(json.dumps(details))
    time.sleep(30)
sys.stdout.buffer.write(pathlib.Path(os.environ["FAKE_STDOUT_FILE"]).read_bytes() if os.environ.get("FAKE_STDOUT_FILE")
                        else base64.b64decode(os.environ.get("FAKE_STDOUT", "")))
sys.stdout.buffer.flush()
sys.stderr.buffer.write(base64.b64decode(os.environ.get("FAKE_STDERR", "")))
sys.stderr.buffer.flush()
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


@pytest.fixture
def launch(tmp_path):
    workspace = tmp_path / "workspace.+ spaces!"
    workspace.mkdir()
    directory = tmp_path / "launch"
    directory.mkdir(mode=0o700)
    binary = tmp_path / "real-native"
    binary.write_text(f"#!{sys.executable}\n" + FAKE)
    binary.chmod(0o700)
    run_file = directory / "run.json"
    run = {"schema_version": 1, "run_id": str(uuid.uuid4()), "launch_id": str(uuid.uuid4()),
           "workspace": str(workspace),
           "models": {"claude": "claude-selected", "codex": "codex-selected", "opencode": "opencode-selected"},
           "native_binaries": {"claude": str(binary), "codex": str(binary), "opencode": str(binary)},
           "state_dir": str(tmp_path / "state"), "provider_path": str(directory)}
    run_file.write_text(json.dumps(run))
    run_file.chmod(0o600)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("OMC_", "CLAUDE", "CHAT_API"))}
    environment.update({"FAKE_CAPTURE": str(tmp_path / "capture.json"),
                        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
                        "CODEX_HOME": str(tmp_path / "codex-home")})
    return {"root": tmp_path, "cwd": workspace, "run": run, "file": run_file, "env": environment}


def command(launch, provider="codex", args=()):
    return [sys.executable, "-B", str(CLI), "--provider", provider,
            "--run-file", str(launch["file"]), "--", *args]


def run_cli(launch, provider="codex", args=(), *, output=b"", stderr=b"", exit_code=0, env=None):
    source = launch["root"] / "fake-stdout.bin"
    source.write_bytes(output)
    environment = {**launch["env"], "FAKE_STDOUT_FILE": str(source),
                   "FAKE_STDERR": base64.b64encode(stderr).decode(), "FAKE_EXIT": str(exit_code), **(env or {})}
    return subprocess.run(command(launch, provider, args), input=b"private stdin\n", capture_output=True,
                          cwd=launch["cwd"], env=environment, timeout=10)


def capture(launch):
    return json.loads(Path(launch["env"]["FAKE_CAPTURE"]).read_text())


def jobs(launch):
    return sorted(Path(launch["run"]["state_dir"]).glob("runs/*/jobs/*.json"))


def job(launch):
    paths = jobs(launch)
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def tty_cli(launch, provider, args=(), *, env=None):
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    environment = {**launch["env"], "FAKE_MODE": "tty", **(env or {})}
    process = subprocess.Popen(command(launch, provider, args), stdin=slave, stdout=slave, stderr=slave,
        cwd=launch["cwd"], env=environment, start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while process.poll() is None:
            assert time.monotonic() < deadline, "private PTY child did not exit"
            if select.select([master], [], [], .1)[0]:
                output.extend(os.read(master, 65536))
        assert process.wait() == 0
        assert termios.tcgetattr(slave) == before
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("args,worker,expected", [
    ([], False, "selected"),
    (["--model", "manual"], False, "manual"),
    (["-m", "first", "--model=manual"], False, "manual"),
    (["--model", "role-generated"], True, "selected"),
    (["--wrapper-model", "private", "--model", "manual"], True, "private"),
    (["--wrapper-model=private"], False, "private"),
])
def test_model_selection_through_public_cli(launch, provider, args, worker, expected):
    result = run_cli(launch, provider, args, env={"OMC_TEAM_WORKER": "team/worker-1", "OMC_TEAM_ROLE": "executor"} if worker else {})
    assert result.returncode == 0, result.stderr
    observed = capture(launch)["args"]
    assert observed == ["--model", provider + "-selected" if expected == "selected" else expected]
    assert job(launch)["requested_model"] == observed[1]
    if worker:
        assert job(launch)["worker"] == "team/worker-1"
        assert job(launch)["role"] == "executor"


def test_prompt_separator_and_permissions_survive(launch):
    args = ["--yolo", "--allowedTools", "Read,Edit,Bash", "--", "--model=prompt-data", "--yolo"]
    result = run_cli(launch, "claude", args)
    assert result.returncode == 0, result.stderr
    assert capture(launch)["args"] == ["--model", "claude-selected", "--dangerously-skip-permissions",
                                        "--allowedTools", "Read,Edit,Bash", "--", "--model=prompt-data", "--yolo"]


def test_binary_output_stdin_cwd_and_exit_are_native(launch):
    stdout, stderr = b"native\x00\xff\nbytes", b"diagnostic\x00\xff"
    result = run_cli(launch, args=["exec", "--yolo", "-"], output=stdout, stderr=stderr, exit_code=23)
    assert result.returncode == 23
    assert result.stdout == stdout
    assert result.stderr.startswith(stderr + b"\n[omc-provider-status] ")
    details, record = capture(launch), job(launch)
    assert details["stdin"] == "private stdin\n"
    assert details["cwd"] == str(launch["cwd"])
    assert details["args"] == ["exec", "--model", "codex-selected", "--yolo", "-"]
    assert record["job_state"] == "failed" and record["exit_code"] == 23
    assert record["provider_status"]["actual_model"] is None
    assert record["provider_status"]["block"]["detected"] is None
    for path in [*jobs(launch), *Path(launch["run"]["state_dir"]).rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_auth_environment_and_parent_identity(launch):
    parent = str(uuid.uuid4())
    result = run_cli(launch, env={"CHAT_API_KEY": "api-secret-fixture", "CLAUDECODE": "nested",
        "CLAUDE_SESSION_ID": "nested", "CLAUDECODE_SESSION_ID": "nested", "CLAUDE_CODE_ENTRYPOINT": "nested",
        "OPENAI_API_KEY": "provider-secret", "ANTHROPIC_API_KEY": "anthropic-secret",
        "ORDINARY_PROVIDER_OPTION": "keep-me", "OMC_WRAPPER_JOB_ID": parent})
    assert result.returncode == 0, result.stderr
    environment = capture(launch)["env"]
    assert not any(key in environment for key in ["CHAT_API_KEY", "CLAUDECODE", "CLAUDE_SESSION_ID",
                                                "CLAUDECODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"])
    assert environment["OPENAI_API_KEY"] == "provider-secret"
    assert environment["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert environment["ORDINARY_PROVIDER_OPTION"] == "keep-me"
    assert environment["PATH"].split(os.pathsep)[0] == launch["run"]["provider_path"]
    assert environment["OMC_WRAPPER_RUN_FILE"] == str(launch["file"])
    assert environment["OMC_WRAPPER_RUN_ID"] == launch["run"]["run_id"]
    assert environment["OMC_WRAPPER_STATE_DIR"] == launch["run"]["state_dir"]
    assert environment["OMC_WRAPPER_JOB_ID"] == job(launch)["job_id"]
    assert job(launch)["parent_job_id"] == parent


@pytest.mark.parametrize("args", [["--version"], ["--help"], ["auth", "status"], ["login"], ["exec", "--help"]])
def test_utility_calls_do_not_add_models_or_jobs(launch, args):
    result = run_cli(launch, "claude", args, output=b"utility output", stderr=b"utility diagnostic")
    assert result.returncode == 0
    assert result.stdout == b"utility output" and result.stderr == b"utility diagnostic"
    assert capture(launch)["args"] == args
    assert not Path(launch["run"]["state_dir"]).exists()


def test_structured_refusal_is_reported_without_retry_or_secret_persistence(launch):
    prompt, secret = "DO-NOT-PERSIST-PROMPT", "known-secret-fixture-value"
    events = [{"type": "user", "message": {"content": prompt}},
              {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": prompt}},
              {"type": "turn.failed", "error": {"code": "cyber_safety_block",
                "message": f"Request blocked by cybersecurity policy. {secret} {prompt}"}}]
    raw = "\n".join(json.dumps(event) for event in events).encode()
    result = run_cli(launch, args=["exec", prompt], output=raw, exit_code=1,
                     env={"OPENAI_API_KEY": secret})
    assert result.returncode == 1 and result.stdout == raw
    record = job(launch)
    assert record["provider_status"]["outcome"] == "blocked"
    assert record["provider_status"]["block"]["category"] == "cybersecurity"
    stored = jobs(launch)[0].read_text()
    assert prompt not in stored and secret not in stored and "private stdin" not in stored
    assert "raw_json" not in record and "argv" not in record and "stdout" not in record
    assert not any("prompt" in key for key in record)


def test_native_fallback_stays_native_and_actual_model_is_observed(launch):
    events = [
        {"type": "system", "subtype": "init", "model": "claude-original"},
        {"type": "system", "subtype": "model_fallback", "from_model": "claude-original",
         "to_model": "claude-effective", "reason": "cybersecurity"},
        {"type": "assistant", "message": {"model": "claude-effective", "content": [{"type": "text", "text": "Advice"}]}},
        {"type": "result", "result": "Advice"},
    ]
    raw = "\n".join(json.dumps(event) for event in events).encode()
    result = run_cli(launch, "claude", ["-p", "prompt"], output=raw)
    assert result.returncode == 0 and result.stdout == raw
    status = job(launch)["provider_status"]
    assert status["actual_model"] == "claude-effective"
    assert status["actual_model_source"] == "assistant_event"
    assert status["model_switches"][0]["reason"] == "cybersecurity"
    assert status["block"]["detected"] is False


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_custom_control_models_are_not_restricted_by_telemetry_regex(launch, provider):
    custom = "custom/claude@preview endpoint"
    run = launch["run"]
    run["models"][provider] = custom
    run["models"]["codex" if provider == "claude" else "claude"] = "unused\ninvalid"
    launch["file"].write_text(json.dumps(run))
    result = run_cli(launch, provider)
    assert result.returncode == 0, result.stderr
    assert capture(launch)["args"] == ["--model", custom]
    assert job(launch)["requested_model"] == custom
    assert job(launch)["provider_status"]["actual_model"] is None


@pytest.mark.parametrize("option,value", [("--append-system-prompt", "--model=literal"),
    ("--append-system-prompt", "--help"), ("--print", "--help"),
    ("--append-system-prompt", "--session-id")])
def test_native_option_operands_are_not_wrapper_controls(launch, option, value):
    result = run_cli(launch, "claude", [option, value])
    assert result.returncode == 0, result.stderr
    arguments = capture(launch)["args"]
    assert arguments[-2:] == [option, value]
    assert arguments[arguments.index("--model") + 1] == "claude-selected"
    assert job(launch)["requested_model"] == "claude-selected"


@pytest.mark.parametrize("provider,args", [("claude", ["--print", "fixture"]), ("codex", ["exec", "fixture"])])
def test_known_plain_final_answer_refusal_is_reported_with_inference_source(launch, provider, args):
    answer = b"We take extra care with some cybersecurity requests. This content can't be shown."
    result = run_cli(launch, provider, args, output=answer)
    assert result.returncode == 0 and result.stdout == answer
    status = job(launch)["provider_status"]
    assert status["outcome"] == "blocked" and status["block"]["source"] == "response_text"
    assert status["actual_model"] is None
    assert "native_final_answer" in job(launch)["telemetry_sources"]
    assert answer.decode() not in jobs(launch)[0].read_text()


def test_private_terminal_is_inherited_and_restored(launch):
    output = tty_cli(launch, "codex")
    details = capture(launch)
    assert details["tty"] == [True, True, True] and details["foreground"]
    assert details["pgid"] == details["sid"] == details["ppid"]
    assert b"omc-provider-status" not in output
    record = job(launch)
    assert record["job_state"] == "exited" and record["exit_code"] == 0
    assert record["provider_status"]["actual_model"] is None
    assert record["provider_status"]["block"]["detected"] is None


def test_claude_reads_only_its_exact_transcript_and_ignores_user_tools(launch):
    events = [
        {"type": "user", "message": {"content": "Request blocked by cybersecurity policy."}},
        {"type": "tool", "model": "unrelated-tool-model", "error": "cyber_safety_block"},
        {"type": "assistant", "message": {"model": "claude-observed", "content": [
            {"type": "tool_use", "input": {"text": "I cannot assist with cybersecurity."}},
            {"type": "text", "text": "Completed advice."}]}},
    ]
    tty_cli(launch, "claude", env={"FAKE_MODE": "claude_trace", "FAKE_EVENTS": json.dumps(events)})
    assert "--session-id" in capture(launch)["args"]
    status = job(launch)["provider_status"]
    assert status["actual_model"] == "claude-observed"
    assert status["block"]["detected"] is False
    assert "claude_transcript" in job(launch)["telemetry_sources"]


@pytest.mark.parametrize("args", [["--continue"], ["--resume", "human-session-name"], ["--no-session-persistence"]])
def test_ambiguous_claude_session_does_not_scan_conversations(launch, args):
    tty_cli(launch, "claude", args)
    assert "--session-id" not in capture(launch)["args"]
    assert job(launch)["provider_status"]["actual_model"] is None
    assert job(launch)["provider_status"]["block"]["detected"] is None


def test_codex_rollout_reports_native_cyber_reroute(launch):
    events = [
        {"type": "turn_context", "payload": {"model": "codex-request-context"}},
        {"type": "event_msg", "payload": {"type": "model_reroute", "from_model": "codex-request-context",
            "to_model": "codex-effective", "reason": "high_risk_cyber_activity"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "content_filter"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Completed advice."}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Completed advice."}},
    ]
    tty_cli(launch, "codex", env={"FAKE_MODE": "codex_trace", "FAKE_EVENTS": json.dumps(events)})
    status = job(launch)["provider_status"]
    assert status["actual_model"] == "codex-effective"
    assert status["actual_model_source"] == "provider_routing_notice"
    assert status["model_switches"][0]["reason"] == "HighRiskCyberActivity"
    assert status["model_switches"][0]["provider_reason"] == "high_risk_cyber_activity"
    assert status["safety_flags"][0]["category"] == "cybersecurity"
    assert status["block"]["detected"] is False
    assert "codex_rollout" in job(launch)["telemetry_sources"]


@pytest.mark.parametrize("mode", ["codex_trace_ambiguous", "codex_trace_wrong_cwd", "codex_trace_outside"])
def test_codex_unattributable_traces_stay_unknown(launch, mode):
    event = {"type": "event_msg", "payload": {"type": "error", "message": "Request blocked by cybersecurity policy."}}
    tty_cli(launch, "codex", env={"FAKE_MODE": mode, "FAKE_EVENTS": json.dumps([event])})
    status = job(launch)["provider_status"]
    assert status["actual_model"] is None and status["block"]["detected"] is None


def test_codex_turn_context_does_not_claim_actual_model(launch):
    event = {"type": "turn_context", "payload": {"model": "configured-only"}}
    tty_cli(launch, "codex", env={"FAKE_MODE": "codex_trace", "FAKE_EVENTS": json.dumps([event])})
    status = job(launch)["provider_status"]
    assert status["initial_model"] == "configured-only"
    assert status["actual_model"] is None and status["actual_model_source"] == "unknown"


@pytest.mark.parametrize("information,blocked,category", [("cyber_policy", True, "cybersecurity"),
    ("bio_policy", True, "safety"), ("misalignment_policy_violation", True, "safety"),
    ("policy_capacity_unavailable", False, None)])
def test_codex_explicit_error_classifiers_preserve_provider_code(launch, information, blocked, category):
    event = {"type": "event_msg", "payload": {"type": "error", "codex_error_info": information,
                                               "message": "Provider could not handle this request."}}
    tty_cli(launch, "codex", env={"FAKE_MODE": "codex_trace", "FAKE_EVENTS": json.dumps([event])})
    status = job(launch)["provider_status"]
    assert status["block"]["detected"] is blocked
    assert status["block"]["category"] == category
    assert status["signals"][0]["code"] == information


def test_native_error_codes_stay_attached_to_their_originating_error(launch):
    events = [
        {"type": "event_msg", "payload": {"type": "error", "code": "transport_error", "message": "Transport unavailable."}},
        {"type": "event_msg", "payload": {"type": "error", "codex_error_info": "cyber_policy", "message": "Policy unavailable."}},
    ]
    tty_cli(launch, "codex", env={"FAKE_MODE": "codex_trace", "FAKE_EVENTS": json.dumps(events)})
    errors = [entry for entry in job(launch)["provider_status"]["signals"] if entry["kind"] == "error"]
    assert [entry["code"] for entry in errors] == ["transport_error", "cyber_policy"]


def test_repeated_answer_and_completion_preserve_two_turn_recovery_order(launch):
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
        {"type": "turn.completed"},
        {"type": "error", "code": "cyber_policy", "message": "Request blocked by cybersecurity safeguards."},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
        {"type": "turn.completed"},
    ]
    result = run_cli(launch, output="\n".join(json.dumps(event) for event in events).encode())
    assert result.returncode == 0
    status = job(launch)["provider_status"]
    assert status["outcome"] == "completed"
    assert status["block"]["detected"] is False
    assert status["safety_flags"][0]["action"] == "recovered"


@pytest.mark.parametrize("signum,expected", [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_signal_exit_finalizes_job_and_native_descendants(launch, signum, expected):
    environment = {**launch["env"], "FAKE_MODE": "signal", "FAKE_DESCENDANT": "1"}
    process = subprocess.Popen(command(launch), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=launch["cwd"], env=environment)
    deadline = time.monotonic() + 5
    details = None
    try:
        while time.monotonic() < deadline:
            try:
                details = capture(launch)
                if details.get("descendant"):
                    break
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(.03)
        assert details and details.get("descendant")
        process.send_signal(signum)
        _, stderr = process.communicate(timeout=6)
        assert process.returncode == expected, stderr
        record = job(launch)
        assert record["job_state"] == "failed" and record["exit_code"] == expected
        assert record["provider_pid"] != record["owner_pid"]
        assert not Path(f"/proc/{record['provider_pid']}").exists()
        status = Path(f"/proc/{details['descendant']}/stat")
        assert not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("invalid", ["profile-mode", "parent-mode", "state-mode", "profile-symlink", "recursive-binary"])
def test_invalid_private_state_fails_before_provider_launch(launch, invalid):
    if invalid == "profile-mode":
        launch["file"].chmod(0o644)
    elif invalid == "parent-mode":
        launch["file"].parent.chmod(0o755)
    elif invalid == "state-mode":
        Path(launch["run"]["state_dir"]).mkdir(mode=0o755)
    elif invalid == "profile-symlink":
        target = launch["file"].with_name("real-run.json")
        launch["file"].rename(target)
        launch["file"].symlink_to(target)
    else:
        shim = launch["file"].parent / "codex"
        shim.symlink_to(launch["run"]["native_binaries"]["codex"])
    result = run_cli(launch)
    assert result.returncode == 2
    assert not Path(launch["env"]["FAKE_CAPTURE"]).exists()
    assert not jobs(launch)


def test_oversized_stdout_is_forwarded_without_unbounded_metadata(launch):
    raw = b'{"type":"user","message":"' + b"x" * 400000 + b'"}\n'
    raw += b'{"type":"item.completed","item":{"type":"agent_message","text":"Advice"}}\n'
    result = run_cli(launch, output=raw)
    assert result.returncode == 0 and result.stdout == raw
    assert jobs(launch)[0].stat().st_size < 10000
    assert job(launch)["provider_status"]["actual_model"] is None


def opencode_run(run=None):
    return {"models": {"claude": "c", "codex": "x", "opencode": "openrouter/z-ai/glm-5.3"}, **(run or {})}


def test_opencode_model_is_injected_only_after_run():
    arguments, selected, utility = prepare_arguments("opencode", ["run", "--format", "json", "hello"], opencode_run(), {})
    assert arguments == ["run", "--model", "openrouter/z-ai/glm-5.3", "--format", "json", "hello"]
    assert (selected, utility) == ("openrouter/z-ai/glm-5.3", False)


def test_opencode_explicit_model_wins_outside_team_workers():
    arguments, selected, _ = prepare_arguments("opencode", ["run", "-m", "ollama/x", "hello"], opencode_run(), {})
    assert selected == "ollama/x"
    assert arguments.count("--model") == 1 and "ollama/x" in arguments


@pytest.mark.parametrize("args", [[], ["serve"], ["models"], ["service", "status"], ["auth", "list"], ["--version"], ["--continue"]])
def test_opencode_tui_and_server_commands_stay_untouched(args):
    arguments, selected, utility = prepare_arguments("opencode", args, opencode_run(), {})
    assert (arguments, selected, utility) == (args, None, True)


def test_opencode_without_a_selection_keeps_its_own_default():
    run = opencode_run({"models": {"opencode": "default"}})
    arguments, selected, utility = prepare_arguments("opencode", ["run", "hello"], run, {})
    assert (arguments, selected, utility) == (["run", "hello"], None, False)


def test_opencode_events_keep_answers_and_errors_only():
    text = {"type": "text", "part": {"text": "PONG", "messageID": "m", "sessionID": "s", "secret": "x"}}
    assert normalize_event("opencode", text) == [{"type": "text", "part": {"text": "PONG", "messageID": "m"}}]
    failure = {"type": "error", "error": {"type": "provider.auth", "message": "nope", "response": {"body": "raw"}}}
    assert normalize_event("opencode", failure) == [{"type": "error", "error": {"type": "provider.auth", "message": "nope"}}]
    assert normalize_event("opencode", {"type": "step_start"}) == []


def test_opencode_run_is_supervised_with_its_selected_model(launch):
    result = run_cli(launch, "opencode", ["run", "--format", "json", "hello"])
    assert result.returncode == 0, result.stderr
    assert capture(launch)["args"] == ["run", "--model", "opencode-selected", "--format", "json", "hello"]
    record = job(launch)
    assert (record["provider"], record["requested_model"], record["job_state"]) == ("opencode", "opencode-selected", "exited")


def test_opencode_tui_is_not_given_a_model_or_a_job(launch):
    result = run_cli(launch, "opencode", ["--continue"])
    assert result.returncode == 0, result.stderr
    assert capture(launch)["args"] == ["--continue"]
