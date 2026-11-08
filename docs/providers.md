# Providers and models

`providers.json` holds what you want. `omc provider apply` renders it into each tool, and every `omc provider` change applies itself. That file is outside the install directory, and `install.sh` re-applies it, so a reinstall or update carries your settings forward.

| Target | What is rendered | Your own files |
| --- | --- | --- |
| OpenCode | `~/.config/opencode/opencode.jsonc`, `commands/model.md`, `commands/provider.md` | `opencode.json` is never edited |
| Claude Code | `~/.claude/skills/provider/`, `~/.claude/agents/omc-route-*.md` | Unrelated files are never touched |
| Hermes | `hermes config set` on the chosen profile | Everything else in `config.yaml` stays |
| Wrapper | The `CHAT_API_*` settings you chose in `service.env` | The key and other lines stay |

Rendered files carry the marker `omc-provider-wrapper generated`. A file at one of these paths without the marker is yours: it is left alone with a warning. The exception is `opencode.jsonc`, which the wrapper needs, so `apply` stops and asks you to move its contents into `opencode.json`.

## OpenCode

OpenCode merges `opencode.jsonc` over `opencode.json`, so the overlay can set the default model, restrict models and add APIs without changing your file. To undo everything, delete the overlay. A value set in the overlay wins, so to change one, use `omc provider`.

**Permitted models.** OpenCode 2 ignores the old `whitelist` and `blacklist` keys, and its `policies` and wildcard model entries do not restrict anything either. The only per-model control it enforces is `disabled: true`, so `allow` lists the live catalog from a private OpenCode server and disables each model you did not permit.

New catalog models are therefore permitted until the next `apply`. `omc provider doctor` reports them.

**Default model.** `/model NAME` and `omc provider model NAME` accept a full `provider/model` or a fragment that matches one permitted model. It applies to new sessions. Use OpenCode's built-in `/models` to switch the current session.

**APIs.** `omc provider opencode --apis` writes `enabled_providers`, which OpenCode enforces for whole providers. `omc provider api add` defines an OpenAI-compatible endpoint. Its models come from `--models`, else from the endpoint's `/models`, else from the last list seen if it is down. Its key is `{env:VAR}`, so the variable must be set for the OpenCode process. For OpenCode's own providers, use `opencode auth login`.

**Direct Ollama.** OpenCode finds the default local Ollama itself. `omc provider ollama --url` points OpenCode, and any Hermes profile using `--api ollama`, somewhere else.

## Routes

`omc provider route add "code review" MODEL` is durable "for X use Y". A running session cannot change its own model, so a route is a subagent pinned to that model, which the orchestrating model picks by task:

- OpenCode gets a `route-*` agent. Its description reaches the model in the subagent tool.
- Claude Code gets an `omc-route-*` agent. For a Claude model, it runs natively. For any other model, a small Haiku agent runs `omc ask opencode`, which is read-only and returns the answer.

Selection is the model's decision, so routing is strong guidance rather than a guarantee. Permitted-model rules are enforced by OpenCode itself.

## Hermes

`--api ID` writes the API as a named Hermes provider and selects it. `--provider NAME` selects a provider already in your Hermes config. Without `--profile`, Hermes uses its active profile. Changes take effect in new Hermes sessions; a running gateway keeps its model until it restarts.

Hermes reads `key_env` from its own `.env` file, not the environment, so put the variable there.

## Updating

`omc provider update` runs `git pull --ff-only` in the checkout you installed from, runs `install.sh`, and re-applies your settings. It refuses to run over uncommitted changes. Pass flags for `install.sh` after it, such as `omc provider update --no-service`.

After upgrading OpenCode, run `omc provider doctor`. It proves the behaviors above against the installed version with a provider that cannot reach a real model.
