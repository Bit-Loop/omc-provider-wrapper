# Installation

## Requirements

Linux, Bash, Python 3.12+, Node 22+, npm, curl, tar, sha256sum, tmux, make and a C++ compiler. Python runtime code uses only the standard library. Your own Claude and Codex accounts are required; authentication is performed by the native CLIs.

For system packages, use your distribution's package manager, for example:

```bash
# Debian/Ubuntu (install Node 22+ separately if the distro Node is older)
sudo apt install python3 curl tar tmux build-essential nodejs npm
# Arch
sudo pacman -S python nodejs npm curl tar tmux base-devel
# Fedora
sudo dnf install python3 nodejs npm curl tar tmux make gcc-c++
```

The installer checks versions before changing configuration. It installs OMC from the commit and SHA-256 in `dependencies.json`, runs its locked production dependency install, and explicitly builds its SQLite/contained-filesystem native components. Existing native Claude and Codex executables are reused. Missing CLIs are installed into a private npm prefix using the versions in `dependencies.json`. OpenCode is optional and is never installed for you; it is used if found on `PATH`.

If matching development headers are absent from the Node installation, the installer downloads the exact running Node version's headers from nodejs.org and checks its published SHA-256. For a custom Node build, set `OMC_WRAPPER_NODE_HEADERS` to an existing directory containing `node_api.h`.

```bash
./install.sh                  # Complete install, user service if systemd is available
./install.sh --no-service     # Configure everything; start API manually
./install.sh --no-providers   # Require existing native Claude and Codex binaries
./install.sh --install-providers # Install pinned private CLIs even if others exist
omc-wrapper-server           # Foreground API when no user systemd session is available
```

Login to newly installed private providers with their installed paths:

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/omc-provider-wrapper/tools/bin/claude" auth login
"${XDG_DATA_HOME:-$HOME/.local/share}/omc-provider-wrapper/tools/bin/codex" login
```

If CLIs were already installed, use `claude auth login` and `codex login`. The installer prints which executables it reused or installed. `omcw` is an alias for the same wrapper.

## Locations and overrides

| Setting | Default |
| --- | --- |
| `OMC_WRAPPER_INSTALL_DIR` | `$XDG_DATA_HOME/omc-provider-wrapper` or `~/.local/share/omc-provider-wrapper` |
| `OMC_WRAPPER_CONFIG_DIR` | `$XDG_CONFIG_HOME/omc-provider-wrapper` or `~/.config/omc-provider-wrapper` |
| `OMC_WRAPPER_BIN_DIR` | `~/.local/bin` |
| `OMC_WRAPPER_PORT` | `8788` |
| `CLAUDE_CONFIG_DIR` | `~/.claude` |
| `CODEX_HOME` | Native Codex default |

Set these before running the installer. Reinstalling preserves the generated `service.env` key and service settings, plus `providers.json` and `models.json`. Use `omc provider update` to update, and `omc provider wrapper` for routing settings. Edit `service.env` locally to change an established port, workspace or provider binary, then restart the service. Never commit it. Default HTTP provider fallback is disabled.

```bash
systemctl --user status omc-provider-wrapper
systemctl --user restart omc-provider-wrapper
node "${XDG_DATA_HOME:-$HOME/.local/share}/omc-provider-wrapper/omc/advisor.mjs" --check
```

Local paths, provider credentials, native conversation transcripts, wrapper status, and advisor artifacts stay on the installing machine. The local bearer credential is randomly generated at installation and stored with mode 0600. OMC artifacts can contain your prompts/responses; treat `.omc/` as private.

## Remove or roll back

```bash
systemctl --user disable --now omc-provider-wrapper.service
```

Remove `omc`, `omcw`, and `omc-wrapper-server` from the chosen bin directory only if they contain the `omc-provider-wrapper launcher` marker. Restore any adjacent `*.before-omc-provider-wrapper-*` launcher you want to use. Remove the wrapper's `status-hook.mjs` entries from Claude settings. Pre-install copies of Claude settings, `CLAUDE.md`, and OMC config are in the wrapper config directory's `backups/` folder; restore them only after reviewing any later changes. OMC setup also adds files under the Claude configuration directory; compare that backup and upstream's setup documentation if removing OMC itself.

To remove the provider integration, delete `~/.config/opencode/opencode.jsonc`, `~/.config/opencode/commands/model.md` and `commands/provider.md`, `~/.claude/skills/provider/`, and `~/.claude/agents/omc-route-*.md`. Hermes keeps the settings written to its config; restore them with `hermes config set`.

Remove the `omc-provider-wrapper.service` unit from `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, and remove the app installation directory. Retain the config/state directories if you want to preserve your local credential and history. Native provider account configuration is managed by the native CLIs.
