#!/usr/bin/env bash
set -euo pipefail
umask 077
source_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
install_dir=${OMC_WRAPPER_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/omc-provider-wrapper}
config_dir=${OMC_WRAPPER_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/omc-provider-wrapper}
bin_dir=${OMC_WRAPPER_BIN_DIR:-$HOME/.local/bin}
claude_dir=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
start_service=yes
install_providers=yes
force_providers=no
port=${OMC_WRAPPER_PORT:-8788}
for arg in "$@"; do
  case "$arg" in
    --no-service) start_service=no ;;
    --no-providers) install_providers=no ;;
    --install-providers) force_providers=yes ;;
    --help|-h) printf '%s\n' 'Usage: ./install.sh [--no-service] [--no-providers|--install-providers]' 'Linux; Python 3.12+, Node 22+, npm, curl, tar, tmux, make and a C++ compiler.' 'Missing native provider CLIs are installed privately. Existing CLIs are reused unless --install-providers is given.'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done
[[ $(uname -s) == Linux ]] || { printf 'This installer supports Linux.\n' >&2; exit 1; }
for tool in python3 node npm curl tar sha256sum tmux make cc c++; do
  command -v "$tool" >/dev/null || { printf 'Missing prerequisite: %s. See docs/install.md.\n' "$tool" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
node -e 'if (+process.versions.node.split(".")[0] < 22) throw Error("Node 22+ required")'
[[ $port =~ ^[0-9]+$ && $port -ge 1024 && $port -le 65535 ]] || { printf 'Invalid OMC_WRAPPER_PORT.\n' >&2; exit 1; }
for value in "$install_dir" "$config_dir" "$bin_dir" "$claude_dir"; do
  [[ $value == /* && $value != *$'\n'* && $value != *'"'* && $value != *'\'* ]] || { printf 'Use absolute installation paths without quotes, backslashes or newlines.\n' >&2; exit 1; }
done
for name in OPENCODE_CONFIG_DIR OMC_WRAPPER_PROVIDER_CONFIG; do
  value=${!name:-}
  [[ -z $value || ( $value == /* && $value != *$'\n'* && $value != *$'\r'* && $value != *'"'* && $value != *'\'* ) ]] || { printf '%s must be an absolute path without double quotes, backslashes or newlines.\n' "$name" >&2; exit 1; }
done
[[ $install_dir != "$source_dir" ]] || { printf 'Install destination must differ from source.\n' >&2; exit 1; }
mkdir -p "$install_dir" "$config_dir" "$bin_dir"
backup_dir="$config_dir/backups/$(date +%Y%m%dT%H%M%S)-$$"
mkdir -p "$backup_dir"
for name in settings.json CLAUDE.md .omc-config.json; do
  if [[ -f $claude_dir/$name ]]; then cp -p -- "$claude_dir/$name" "$backup_dir/$name"; fi
done
for name in agent_chat_api omc scripts patches; do
  mkdir -p "$install_dir/$name"
  tar -C "$source_dir/$name" --exclude=__pycache__ --exclude=runtime --exclude=.pytest_cache -cf - . | tar -C "$install_dir/$name" -xf -
done
cp -- "$source_dir/dependencies.json" "$source_dir/LICENSE" "$install_dir/"
chmod +x "$install_dir/omc/provider-cli.sh"
pin=$(node -p 'require(process.argv[1]).omc.commit' "$source_dir/dependencies.json")
checksum=$(node -p 'require(process.argv[1]).omc.archive_sha256' "$source_dir/dependencies.json")
if [[ ! -f $install_dir/upstream/.wrapper-pin || $(cat "$install_dir/upstream/.wrapper-pin") != "$pin" ]]; then
  printf 'Downloading pinned OMC %s...\n' "$pin"
  scratch=$(mktemp -d "$install_dir/.upstream-XXXXXXXX")
  trap 'rm -rf -- "$scratch"' EXIT
  curl -fL --retry 2 --silent --show-error "https://codeload.github.com/Yeachan-Heo/oh-my-claudecode/tar.gz/$pin" -o "$scratch/omc.tar.gz"
  printf '%s  %s\n' "$checksum" "$scratch/omc.tar.gz" | sha256sum -c -
  mkdir "$scratch/payload"
  tar -xzf "$scratch/omc.tar.gz" --strip-components=1 -C "$scratch/payload"
  if [[ -d $install_dir/upstream ]]; then mv -- "$install_dir/upstream" "$install_dir/upstream.before-$(date +%s)"; fi
  mv -- "$scratch/payload" "$install_dir/upstream"
  printf '%s\n' "$pin" > "$install_dir/upstream/.wrapper-pin"
  rm -rf -- "$scratch"
  trap - EXIT
fi
node_version=$(node -p 'process.versions.node')
node_headers=${OMC_WRAPPER_NODE_HEADERS:-$(node -p 'require("node:path").resolve(process.execPath, "../../include/node")')}
if [[ ! -f $node_headers/node_api.h ]]; then
  if [[ -n ${OMC_WRAPPER_NODE_HEADERS:-} ]]; then printf 'OMC_WRAPPER_NODE_HEADERS must contain node_api.h.\n' >&2; exit 1; fi
  node_headers="$install_dir/node-headers/$node_version/include/node"
  if [[ ! -f $node_headers/node_api.h ]]; then
    printf 'Downloading matching Node %s development headers...\n' "$node_version"
    scratch=$(mktemp -d "$install_dir/.headers-XXXXXXXX")
    trap 'rm -rf -- "$scratch"' EXIT
    header_name="node-v$node_version-headers.tar.gz"
    curl -fL --retry 2 --silent --show-error "https://nodejs.org/dist/v$node_version/$header_name" -o "$scratch/$header_name"
    curl -fL --retry 2 --silent --show-error "https://nodejs.org/dist/v$node_version/SHASUMS256.txt" -o "$scratch/SHASUMS256.txt"
    header_checksum=$(node -e 'const fs=require("node:fs"); const line=fs.readFileSync(process.argv[1],"utf8").split("\n").find(x=>x.endsWith("  "+process.argv[2])); if(!line || !/^[a-f0-9]{64}  /.test(line)) throw Error("Node header checksum missing"); console.log(line.slice(0,64))' "$scratch/SHASUMS256.txt" "$header_name")
    printf '%s  %s\n' "$header_checksum" "$scratch/$header_name" | sha256sum -c -
    mkdir -p "$install_dir/node-headers/$node_version"
    tar -xzf "$scratch/$header_name" --strip-components=1 -C "$install_dir/node-headers/$node_version"
    rm -rf -- "$scratch"
    trap - EXIT
  fi
fi
printf 'Installing locked OMC runtime dependencies...\n'
(cd "$install_dir/upstream" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund)
node "$install_dir/scripts/prepare-omc.mjs" "$install_dir/upstream"
(cd "$install_dir/upstream" && npm rebuild better-sqlite3 --no-audit --no-fund && node scripts/build-contained-fs.mjs --force --headers="$node_headers")
node --input-type=module - "$install_dir/upstream" <<'JS'
import { createRequire } from 'node:module';
import { join } from 'node:path';
const require = createRequire(join(process.argv[2], 'package.json'));
const Database = require('better-sqlite3');
const db = new Database(':memory:'); db.close();
require('@ast-grep/napi');
require(join(process.argv[2], 'native', `contained-fs-${process.platform}-${process.arch}.node`));
JS
resolve_provider() {
  local name=$1 candidate resolved
  candidate=$(command -v "$name" || true)
  if [[ $force_providers == no && -n $candidate && -x $candidate ]]; then
    resolved=$(readlink -f -- "$candidate")
    if [[ $resolved != */provider-cli.sh && $resolved != */native_cli.py ]]; then printf '%s\n' "$resolved"; return; fi
  fi
  if [[ $install_providers == no ]]; then printf 'Native %s CLI missing.\n' "$name" >&2; return 1; fi
  printf 'Installing native %s CLI privately...\n' "$name" >&2
  local package
  package=$(node -p 'require(process.argv[1]).providers[process.argv[2]]' "$source_dir/dependencies.json" "$name")
  npm install --prefix "$install_dir/tools" --ignore-scripts --no-audit --no-fund "$package" >&2
  if [[ $name == claude ]]; then
    node "$install_dir/tools/node_modules/@anthropic-ai/claude-code/install.cjs" >&2
  fi
  # npm uses node_modules/.bin for a local prefix.
  mkdir -p "$install_dir/tools/bin"
  ln -sfn -- "$install_dir/tools/node_modules/.bin/$name" "$install_dir/tools/bin/$name"
  readlink -f -- "$install_dir/tools/bin/$name"
}
native_claude=$(resolve_provider claude)
native_codex=$(resolve_provider codex)
native_opencode=$(command -v opencode || true)
if [[ -z $native_opencode ]]; then printf 'OpenCode not found; continuing without it. Install it, then rerun this script.\n'; fi
for provider in claude codex; do
  if [[ -z $(command -v "$provider" || true) && ! -e $bin_dir/$provider && ! -L $bin_dir/$provider ]]; then
    native_path=$native_claude
    [[ $provider != codex ]] || native_path=$native_codex
    ln -s -- "$native_path" "$bin_dir/$provider"
  fi
done
"$native_claude" --version
"$native_codex" --version
export CLAUDE_CONFIG_DIR="$claude_dir"
export OMC_PLUGIN_ROOT="$install_dir/upstream"
printf 'Configuring OMC hooks and plugin support (backups: %s)...\n' "$backup_dir"
node "$install_dir/upstream/bridge/cli.cjs" setup --plugin-dir-mode --quiet
node "$install_dir/scripts/configure-install.mjs" "$install_dir" "$config_dir" "$bin_dir" "$native_claude" "$native_codex" "$(command -v python3)" "$claude_dir" "$port" "$native_opencode" "$source_dir"
if [[ $start_service == yes ]]; then
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now omc-provider-wrapper.service
    systemctl --user restart omc-provider-wrapper.service
  else
    printf 'No user systemd session. Start the API with: %s/omc-wrapper-server\n' "$bin_dir"
  fi
fi
# Settings outlive the install; re-rendering carries them onto the new version.
OMC_WRAPPER_CONFIG_DIR="$config_dir" node "$install_dir/omc/omc.mjs" provider apply || printf 'Run omc provider doctor: provider settings were not re-applied.\n' >&2
printf '\nInstalled. Add %s to PATH, then run:\n' "$bin_dir"
printf '  omc models select\n  omc provider doctor\n  omc launch\n  omc wrapper status\n'
printf 'Provider login: claude auth login; codex login (your own accounts).\n'
