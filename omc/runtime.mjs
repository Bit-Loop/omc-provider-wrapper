import { readFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { installation } from './installation.mjs';

export function pluginRoot(env = process.env) {
  const managed = env.OMC_WRAPPER_PLUGIN_ROOT || installation(env).omc_root;
  if (managed) {
    if (!existsSync(join(managed, 'bridge', 'cli.cjs'))) throw new Error('Managed OMC is missing. Run install.sh again.');
    return resolve(managed);
  }
  const configured = env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude');
  const configDir = configured === '~' ? homedir()
    : configured.startsWith('~/') ? join(homedir(), configured.slice(2)) : resolve(configured);
  const registry = JSON.parse(readFileSync(join(configDir, 'plugins', 'installed_plugins.json'), 'utf8'));
  const entry = registry.plugins?.['oh-my-claudecode@omc']?.find(item => item.scope === 'user');
  if (!entry || !existsSync(join(entry.installPath, 'bridge', 'cli.cjs'))) {
    throw new Error('OMC user plugin is missing. Install oh-my-claudecode@omc with Claude Code.');
  }
  return entry.installPath;
}
