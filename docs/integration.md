# Wrapper and OMC integration

## Included code

- `agent_chat_api/`: standalone standard-library HTTP wrapper, native CLI supervisor, provider event parsing/redaction, and tests.
- `omc/`: OMC frontend, advisor adapter, model controls, provider configuration, launch check, native provider shim, and Claude status hook.
- `scripts/` and `install.sh`: relocatable installation, local service configuration and dependency preparation.

The export contains source and fixtures with a new Git history. It excludes account configuration, credentials, service environment files, runtime state, transcripts, local project briefs, and unrelated application code.

## OMC changes reproduced by this package

The installer downloads upstream OMC **5.7.31**, commit `0564555b63dfedd158669c6799d53743abd7d6bf`, verified by SHA-256. Its dependencies remain pinned by upstream `package-lock.json`. OMC is MIT licensed; see [third-party notices](third-party.md).

All custom integration code is included here. The bundled [Linux compiler fix](../patches/omc-contained-fs-linux.patch) moves two macOS-only variable declarations inside their platform guard so upstream's `-Werror` build succeeds on Linux. `scripts/prepare-omc.mjs` applies that exact change to the pinned source, then reproduces the local installation changes using upstream helpers:

1. Compact plugin skills into shims plus `skill-bodies/`.
2. Normalize plugin hook commands for portable Node discovery.
3. Approve only the locked `better-sqlite3` install script, then explicitly rebuild and verify its native module. Force upstream's contained-filesystem build on Linux and verify that module and `@ast-grep/napi` load locally.

`omc setup --plugin-dir-mode` configures upstream hooks, HUD and `CLAUDE.md`. The wrapper launches the pinned local plugin using upstream `--plugin-dir`. This avoids replacing an unrelated marketplace checkout. Existing settings and launchers are backed up.

The standalone export also includes HTTP fixes found during publication review: streaming drains both pipes concurrently, enforces the prepared deadline, and reaps the process group on timeout/cancellation; malformed message elements return a request error. Native telemetry uses the public package's XDG state directory. Tests cover these changes.

## Provider routes

| Route | Execution | Permissions |
| --- | --- | --- |
| `omc launch`, CLI team workers | Private per-launch `claude`/`codex` shims → native CLI supervisor → real binaries | Native CLI tools, permission flags and terminal behavior |
| `omc ask claude` / `omc ask codex` | Upstream advisor-script hook → loopback authenticated HTTP wrapper | Read-only, tools disabled, no automatic cross-provider fallback |
| `omc ask opencode` | Wrapper frontend → loopback authenticated HTTP wrapper → `opencode run --standalone` | Deny-all agent, so the model receives no tools |
| Other OMC advisors | Upstream advisor script | Upstream behavior |

Run profiles pin model selections and real binary paths, so OMC workers retain routing even when upstream filters environment variables. Profiles and status files are private; provider commands, model IDs and provider-auth configuration are local inputs. Do not share run profiles or status files blindly.

Team startup is supervised by the wrapper. A reported startup failure is often a
pane-busy false alarm rather than a dead worker, so the wrapper first settles it with
cheap local reads (`list-tasks`, `read-worker-heartbeat`) for a bounded grace window: a
worker that has claimed its task or written a fresh heartbeat is reported as
`already_running` (`detectedBy:startup-settle`) and enters the normal wait path with no
recovery. This avoids the brokered `recover-worker` round trip, which upstream caps at
180-300s and which can only time out against a live worker while leaving a dangling
recovery request. Only a worker that stays silent through the window gets one immediate
`recover-worker` attempt (bounded just past the upstream cap) while healthy workers keep
their tasks and panes. The settle window is tunable with `OMC_TEAM_STARTUP_SETTLE_MS`
(default 30000), `OMC_TEAM_STARTUP_SETTLE_INTERVAL_MS` (default 1000) and
`OMC_TEAM_HEARTBEAT_MAX_AGE_MS` (default 45000). JSON startup results include
`startupRecovery`, `waitAllowed` and `nextAction`; unresolved startup returns nonzero
with `waitAllowed:false` and `nextAction:recover-or-reassign`. Only confirmed
startup/recovery enters the ordinary wait path. Preserve launch exit status instead of
piping it through `tail` or `head`.

Recovery reuses the owner-private run profile adjacent to the worker's recorded
provider shim. Creating a fresh command profile would change the shim path and
trigger OMC's `launch_descriptor_unresolvable` check. The recorded model, provider
and workspace are retained; upstream pane/process ownership checks remain in force.
The custom OpenCode launcher, service and credentials are not replaced by this path.

`omc models set <claude|codex|opencode> MODEL` and interactive `select` save defaults. Put `--claude-model`, `--codex-model` and `--opencode-model` before any OMC command for per-run selections. See [providers and models](providers.md) for OpenCode, Hermes and Ollama. `ask` also accepts leading `--model` and `--effort`. Custom IDs are passed through; availability and entitlement are decided by the provider. Native in-process Claude subagents retain Claude's own model controls (`CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`).

OpenCode's upstream `ask` is unavailable because its provider list is closed, so the frontend parses the same prompt grammar and calls the advisor itself. Requests use a private server because the user's background service has its own environment. `OPENCODE_NO_AUTOLLAMA=1` stops launcher scripts from starting a GPU model for a non-interactive request. Under `omc launch`, only `opencode run` is supervised and given a model; the TUI and server commands pass through unchanged.

## Signals and limits

The wrapper records requested models separately from observed actual models. Provider JSON events and attributable native session logs can report actual models, switches, policy blocks, and recovery. The status hook adds changed observations to the Claude orchestrator's context. Advisors emit status metadata alongside private artifacts.

OpenCode's JSON events expose answers and typed errors but not the model that answered, so its actual model stays unknown. Typed Codex policy errors and exposed cyber reroute events are recognized; refusal prose is marked as inferred. Capacity errors remain distinct. Recovered errors do not turn successful final results into failures. Missing, ambiguous or inaccessible telemetry stays unknown. No wrapper can promise detection of a server-side decision the provider does not expose. The wrapper does not force an Opus downgrade or automatically retry another provider to evade a policy block.
