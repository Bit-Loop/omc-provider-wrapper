import { readFileSync, realpathSync, accessSync, constants } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve, delimiter } from 'node:path';

export function configRoot(env = process.env, home = homedir()) {
  return resolve(env.OMC_WRAPPER_CONFIG_DIR || join(env.XDG_CONFIG_HOME || join(home, '.config'), 'omc-provider-wrapper'));
}

function expandHome(value, home) {
  return value === '~' ? home : value.startsWith('~/') ? join(home, value.slice(2)) : resolve(value);
}

export function claudeDir(env = process.env, home = homedir()) {
  return expandHome(env.CLAUDE_CONFIG_DIR || join(home, '.claude'), home);
}

export function openCodeDir(env = process.env, home = homedir()) {
  return resolve(env.OPENCODE_CONFIG_DIR || join(env.XDG_CONFIG_HOME || join(home, '.config'), 'opencode'));
}

export function installation(env = process.env, home = homedir()) {
  try { return JSON.parse(readFileSync(join(configRoot(env, home), 'install.json'), 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return {}; throw error; }
}

export function nativeBinary(provider, env = process.env, home = homedir(), inherited = {}) {
  const saved = installation(env, home).native_binaries || {};
  const explicit = env[`OMC_WRAPPER_${provider.toUpperCase()}_BIN`] || inherited[provider] || saved[provider];
  const candidates = explicit ? [explicit] : (env.PATH || '').split(delimiter).filter(Boolean).map(dir => join(dir, provider));
  for (const candidate of candidates) {
    try {
      accessSync(candidate, constants.X_OK);
      const actual = realpathSync(candidate);
      if (actual.endsWith('/provider-cli.sh') || actual.endsWith('/native_cli.py')) continue;
      return actual;
    } catch { /* Continue searching PATH. */ }
  }
  if (provider === 'opencode') return null;
  throw new Error(`Native ${provider} CLI missing. Run install.sh or set OMC_WRAPPER_${provider.toUpperCase()}_BIN.`);
}

export function nativeBinaries(env = process.env, home = homedir(), inherited = {}) {
  // Native runs still require Claude and Codex; optional provider configuration
  // resolves only its own binary and must not require unrelated installed CLIs.
  return Object.fromEntries(['claude', 'codex', 'opencode'].flatMap(provider => {
    const binary = nativeBinary(provider, env, home, inherited);
    return binary ? [[provider, binary]] : [];
  }));
}
