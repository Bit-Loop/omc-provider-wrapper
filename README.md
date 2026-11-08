![OMC Provider Wrapper — Claude + Codex. Your models. Clear signals.](assets/header.png)

# OMC Provider Wrapper

Run **Claude, Codex and OpenCode through one local wrapper** with [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode). Choose models per provider or per run, keep native terminal tools working, and send observed model changes, policy blocks, and recovery signals back to the Claude orchestrator. `omc provider` configures OpenCode, Hermes, direct Ollama and the wrapper from one durable file.

## Install

Linux · Python 3.12+ · Node 22+ · npm · tmux · curl · make · C++ compiler.

```bash
git clone https://github.com/Bit-Loop/omc-provider-wrapper.git
cd omc-provider-wrapper
./install.sh
export PATH="$HOME/.local/bin:$PATH"
claude auth login
codex login
```

The installer fetches pinned OMC and its locked dependencies, installs missing provider CLIs, creates a private local credential, and starts a user service. Existing Claude settings and replaced launchers are backed up. [Install options and removal →](docs/install.md)

## Use

```bash
# Pick your defaults interactively; exact custom model IDs are accepted.
omc models select
omc models show

# Or choose both models for one run.
omc --claude-model opus --codex-model YOUR_CODEX_MODEL launch

# Ask a provider through the local HTTP wrapper.
omc ask claude --model sonnet --prompt "Review this design"
omc ask codex --model YOUR_CODEX_MODEL --prompt "Check this approach"
omc ask opencode --model openrouter/z-ai/glm-5.3 --prompt "Find the risk here"

# See provider outcomes and observed model information.
omc wrapper status --json
```

Durable goals work with the same wrapper:

```bash
omc ultragoal create-goals --brief-file YOUR_BRIEF.md --claude-goal-mode aggregate
omc --claude-model opus --codex-model YOUR_CODEX_MODEL launch --yolo
```

`--yolo` enables native permission bypass. Native CLI sessions retain native tools; HTTP advisors use read-only settings. The wrapper reports provider signals when exposed; unknown model or block information stays unknown. It does not force model downgrades or route around policy blocks.

## Providers and models

`/provider` and `/model` work inside OpenCode. `/provider` works inside Claude Code; Claude's built-in `/model` still controls Claude's own session. You can also just ask either one, for example "for code review use glm-5.3". The `provider` skill turns that into the command below, so it persists.

```bash
omc provider                                   # Everything that is configured
omc provider doctor                            # Check it all, including that OpenCode still enforces it
omc provider model glm-5.3                     # Default OpenCode model (/model glm-5.3)
omc provider models glm                        # Find a model
omc provider allow set 'openrouter/z-ai/*' ollama/qwen3:30b  # Permitted models; `allow all` lifts it
omc provider route add "code review" openrouter/z-ai/glm-5.3 # "For X use Y"
omc provider opencode --apis openrouter,ollama # Which APIs OpenCode may use
omc provider api add local http://127.0.0.1:8081/v1 --key-env LOCAL_KEY
omc provider hermes --profile local-code --api ollama --model qwen3:30b
omc provider ollama --url http://127.0.0.1:11434/v1
omc provider wrapper --default-provider opencode
omc provider update                            # Pull, reinstall, re-apply
```

Settings live in `~/.config/omc-provider-wrapper/providers.json`, outside the install directory, so updates cannot lose them. API keys are never stored there; `--key-env` takes a variable name. [How it works →](docs/providers.md)

[Architecture & OMC changes](docs/integration.md) · [Testing](docs/testing.md) · [MIT license](LICENSE)
