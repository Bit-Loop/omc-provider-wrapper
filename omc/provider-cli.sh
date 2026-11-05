#!/bin/sh
# Each launch has an adjacent private profile; preserve this symlink's $0.
omc_wrapper_provider=${0##*/}
omc_wrapper_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
omc_wrapper_source=$(readlink -f -- "$0") || exit 1
exec python3 -B "$(dirname -- "$omc_wrapper_source")/../agent_chat_api/native_cli.py" \
  --provider "$omc_wrapper_provider" --run-file "$omc_wrapper_directory/run.json" -- "$@"
