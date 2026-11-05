#!/usr/bin/env bash
# Offline provider fixture. This is not a provider credential or live model.
set -euo pipefail
case " $* " in
  *' --version '*) printf 'Fixture provider 1.0.0\n'; exit 0 ;;
  *' auth status '*) printf '{"loggedIn":true,"authMethod":"fixture"}\n'; exit 0 ;;
  *' login status '*) printf 'Logged in using ChatGPT (offline fixture)\n'; exit 0 ;;
esac
cat >/dev/null
if [[ ${0##*/} == codex ]]; then
  printf '%s\n' '{"type":"thread.started","thread_id":"00000000-0000-4000-8000-000000000001"}' '{"type":"item.completed","item":{"type":"agent_message","text":"OFFLINE_WRAPPER_OK"}}' '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
else
  printf '%s\n' '{"type":"system","subtype":"init","model":"claude-fixture"}' '{"type":"result","subtype":"success","is_error":false,"result":"OFFLINE_WRAPPER_OK","modelUsage":{"claude-fixture":{"inputTokens":1,"outputTokens":1}}}'
fi
