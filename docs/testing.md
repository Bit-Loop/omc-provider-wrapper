# Testing

Runtime tests use offline provider fixtures, private temporary homes, and local loopback servers. They do not need provider accounts or make model calls.

`node --test omc/team-recovery.test.mjs` exercises the real wrapper frontend against
an offline OMC fixture. It covers recorded-profile reuse, immediate failed-start
recovery, a pane-busy false alarm settling via local reads instead of brokered
recovery, healthy-worker preservation, an unresolved recovery returning no-wait
guidance, and owner-private profile validation. Native routing tests separately
exercise the installed binaries with private fixtures.

`node --test scripts/prepare-omc.test.mjs` checks the actual pinned module and
bundles for consistent detached window sizing, the v2 leader-only startup count,
repeatable preparation, and rejection of unfamiliar code before any team entry
point changes. Real private tmux sessions exercise ten-worker layouts through
both preallocation and the v2 leader-only allocation pattern, without model calls.
Live validation should separately require five Claude workers to complete five
distinct tasks, with no startup failures or manual window resizing, then shut
the team down and verify its panes and runtime owner are gone.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -B -m pytest -q
npm test
```

OMC integration tests require a configured installation or `OMC_WRAPPER_PLUGIN_ROOT` pointing at the pinned OMC payload with its production dependencies. The installer is exercised against a fresh home/config/data directory in CI; the exact downloaded archive is SHA-256 checked. Actual native CLIs are installed and version-checked, then offline fixtures verify HTTP authentication, both provider routes and readiness. Native routing tests verify tool flags, model selection, filtered worker environments, and a private tmux session.

For the installed offline smoke check, set `OMC_WRAPPER_CONFIG_DIR` and `OMC_WRAPPER_BIN_DIR` to an isolated installation, then run `node scripts/smoke-installed.mjs`. Do not point this smoke check at an API already running on the same port.

`omc/opencode-contract.test.mjs` runs the real OpenCode binary against a local mock, with no account or network. It pins what the wrapper depends on: overlay precedence, `disabled` enforcement, `enabled_providers`, the zero-tool advisor agent, and command shell steps. It runs when `opencode` is on `PATH` or `OMC_WRAPPER_OPENCODE_BIN` is set, and reports as skipped otherwise. Rerun it after any OpenCode upgrade.

Run `npm test` outside Claude Code. Two launch tests fail inside a session with `Already inside a Claude Code session`; unset `CLAUDECODE` to run them there.

Provider-model entitlement, live server-side routing decisions, and future native CLI event formats require separate live validation. Tests distinguish exposed metadata from unknown values.
