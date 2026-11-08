---
name: provider
description: Configure and inspect OpenCode, Hermes, direct Ollama, the OMC provider wrapper and model routing. Use for /provider, or when the user asks which model is in use, changes the default model, sets permitted models, says "for X tasks use Y model", adds or switches an API endpoint, or updates the wrapper.
---
<!-- omc-provider-wrapper generated -->
# Provider and model configuration

`omc provider` owns this configuration. Its settings survive wrapper updates, and it regenerates every derived file. Never edit `opencode.jsonc`, `providers.json`, or a `route-*` agent by hand; a hand edit is overwritten and a `whitelist` key does nothing in OpenCode 2.

Pick the command from what the user wants:

| Request | Command |
| --- | --- |
| What is configured? | `omc provider show` |
| Is it all working? | `omc provider doctor` |
| Find a model | `omc provider models [FILTER]` |
| Make X the OpenCode default | `omc provider model X` |
| Only allow these models | `omc provider allow set MODEL...` (`*` wildcards work) |
| Permit or stop permitting one model | `omc provider allow add MODEL...` / `allow remove MODEL...` |
| Permit everything again | `omc provider allow all` |
| "For X tasks use Y" | `omc provider route add "X" Y` |
| Forget that rule | `omc provider route remove "X"` |
| Limit OpenCode to some APIs | `omc provider opencode --apis openrouter,ollama` (`all` lifts it) |
| Add an OpenAI-compatible endpoint | `omc provider api add ID URL [--key-env VAR] [--models a,b]` |
| Point at another Ollama | `omc provider ollama --url URL` |
| Change Hermes | `omc provider hermes [--profile P] [--model M] [--api ID \| --provider NAME]` |
| Change wrapper routing | `omc provider wrapper [--default-provider P] [--fallback a,b] [--fallback-enabled true\|false] [--timeout N]` |
| Update the wrapper | `omc provider update` |

Rules:

1. Run the command rather than describing it, then confirm using its output.
2. Resolve a loose model name first: run `omc provider models NAME`. If several match, ask the user which one.
3. API keys never go in commands. `--key-env` takes a variable name. `opencode auth login` stores OpenCode's own keys.
4. A new default applies to new OpenCode sessions. In the current session, the user switches with `/models`.
5. A route model is `provider/model` or a Claude model such as `sonnet`. Routes work by delegation: OpenCode and Claude Code each get a subagent pinned to that model, chosen by matching the task. A route does not switch the model of the session you are in. Claude Code's own session model stays under its built-in `/model`.
