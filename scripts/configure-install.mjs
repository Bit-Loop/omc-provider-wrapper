#!/usr/bin/env node
import { readFileSync, writeFileSync, existsSync, mkdirSync, chmodSync, renameSync, lstatSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { randomBytes, randomUUID } from 'node:crypto';

const [root, cfg, bin, claude, codex, python, configDir, port] = process.argv.slice(2).map(String);
const state = resolve(process.env.XDG_STATE_HOME || join(homedir(), '.local/state'), 'omc-provider-wrapper');
const envFile = join(cfg, 'service.env');
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
const unitQuote = value => '"' + value.replaceAll('\\', '\\\\').replaceAll('"', '\\"').replaceAll('%', '%%') + '"';
const unitExec = value => unitQuote(value.replaceAll('$', '$$'));
for (const dir of [cfg, state, join(state, 'workspace'), bin]) mkdirSync(dir, { recursive: true, mode: 0o700 });
chmodSync(cfg, 0o700);
function privateWrite(filename, content) {
  if (existsSync(filename) && lstatSync(filename).isSymbolicLink()) throw new Error('Refusing to replace a symlink configuration file.');
  const temp = filename + '.' + randomUUID();
  writeFileSync(temp, content, { mode: 0o600, flag: 'wx' });
  renameSync(temp, filename);
}
if (!existsSync(envFile)) {
  const values = { CHAT_API_KEY: randomBytes(32).toString('hex'), CHAT_API_HOST: '127.0.0.1', CHAT_API_PORT: port,
    CHAT_API_WORKSPACE_ROOT: join(state, 'workspace'), CHAT_API_CODEX_BIN: codex, CHAT_API_CLAUDE_BIN: claude,
    CHAT_API_DEFAULT_PROVIDER: 'codex', CHAT_API_PROVIDER_FALLBACK_ENABLED: 'false',
    CHAT_API_FALLBACK_PROVIDERS: 'codex', CHAT_API_SESSION_STORE: join(state, 'sessions.json'),
    CHAT_API_AUDIT_LOG: join(state, 'provider-audit.jsonl'),
    CLAUDE_CONFIG_DIR: configDir,
    ...(process.env.CODEX_HOME ? { CODEX_HOME: resolve(process.env.CODEX_HOME) } : {}),
    PATH: [join(root, 'tools/bin'), process.env.PATH || '/usr/local/bin:/usr/bin:/bin'].join(':') };
  if (Object.values(values).some(value => /[\r\n"\\]/.test(value))) throw new Error('Installation paths cannot contain newlines, quotes or backslashes.');
  privateWrite(envFile, Object.entries(values).map(([key, value]) => `${key}="${value}"`).join('\n') + '\n');
}
chmodSync(envFile, 0o600);
// A reinstall preserves the key, port and service settings already in use.
const previous = readFileSync(envFile, 'utf8').match(/^CHAT_API_PORT="?(\d+)"?$/m);
const actualPort = previous?.[1] || port;
privateWrite(join(cfg, 'install.json'), JSON.stringify({ schema_version: 1, root, omc_root: join(root, 'upstream'),
  native_binaries: { claude, codex }, python, service_env: envFile, url: `http://127.0.0.1:${actualPort}` }, null, 2) + '\n');
const settingsFile = join(configDir, 'settings.json');
mkdirSync(configDir, { recursive: true, mode: 0o700 });
const settings = existsSync(settingsFile) ? JSON.parse(readFileSync(settingsFile, 'utf8')) : {};
settings.hooks ||= {};
const hookCommand = `${quote(process.execPath)} ${quote(join(root, 'omc/status-hook.mjs'))}`;
for (const event of ['PostToolUse', 'UserPromptSubmit']) {
  settings.hooks[event] ||= [];
  const registered = settings.hooks[event].some(group => group.hooks?.some(hook => hook.command === hookCommand));
  if (!registered) settings.hooks[event].push({ matcher: '', hooks: [{ type: 'command', command: hookCommand, timeout: 5 }] });
}
privateWrite(settingsFile, JSON.stringify(settings, null, 2) + '\n');
for (const [command, target] of [['omc', 'omc/omc.mjs'], ['omcw', 'omc/omc.mjs'], ['omc-wrapper-server', 'scripts/server.mjs']]) {
  const launcher = join(bin, command);
  if (existsSync(launcher) || (() => { try { lstatSync(launcher); return true; } catch { return false; } })()) {
    const managed = (() => { try { return readFileSync(launcher, 'utf8').includes('# omc-provider-wrapper launcher'); } catch { return false; } })();
    if (!managed) renameSync(launcher, launcher + '.before-omc-provider-wrapper-' + Date.now());
  }
  privateWrite(launcher, `#!/bin/sh\n# omc-provider-wrapper launcher\nexport OMC_WRAPPER_CONFIG_DIR=${quote(cfg)}\nexport PATH=${quote(join(root, 'tools/bin'))}:"$PATH"\nexec ${quote(process.execPath)} ${quote(join(root, target))} "$@"\n`);
  chmodSync(launcher, 0o755);
}
const unitDir = join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'systemd/user');
mkdirSync(unitDir, { recursive: true, mode: 0o700 });
const unit = `[Unit]\nDescription=OMC provider wrapper local chat API\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nWorkingDirectory=${join(state, 'workspace').replaceAll('%', '%%')}\nEnvironmentFile=${envFile.replaceAll('%', '%%')}\nUnsetEnvironment=OPENAI_API_KEY ANTHROPIC_API_KEY\nExecStart=${unitExec(python)} -B ${unitExec(join(root, 'agent_chat_api/app.py'))}\nRestart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=15\nUMask=0077\n\n[Install]\nWantedBy=default.target\n`;
privateWrite(join(unitDir, 'omc-provider-wrapper.service'), unit);
console.log(`Configured private settings in ${cfg}`);
