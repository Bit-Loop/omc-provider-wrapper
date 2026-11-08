#!/usr/bin/env node
import { join } from 'node:path';
import { readFileSync, realpathSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { pluginRoot } from './runtime.mjs';
import { checkWrappedClaude, isNativeLaunch } from './launch-preflight.mjs';
import { installation } from './installation.mjs';
import { PROVIDERS, modelConfigPath, savedModels, selectedModels, saveModels, knownModels,
  extractSelectors, createRunProfile, wrapperStatus, selector, OPENCODE_NATIVE_DEFAULT } from './models.mjs';
import { applyProviderConfig } from './provider-apply.mjs';
import { providerCommand } from './provider-command.mjs';
import { isTeamStart, startTeamWithRecovery, teamRecoveryProfile } from './team-recovery.mjs';

// Upstream's ask rejects providers outside its closed list, but its prompt
// grammar applies unchanged, so only the provider name is substituted to parse.
async function askOpenCode(askArgs, root, advisor) {
  const { parseAskArgs } = await import(pathToFileURL(join(root, 'dist/cli/ask.js')).href);
  const { prompt, agentPromptRole } = parseAskArgs(['claude', ...askArgs]);
  const role = agentPromptRole ? readFileSync(join(root, 'agents', `${agentPromptRole.replace(/[^A-Za-z0-9_-]/g, '')}.md`), 'utf8').trim() : '';
  const child = spawnSync(process.execPath, [advisor, 'opencode', role ? `${role}\n\n${prompt}` : prompt], {
    stdio: 'inherit', env: { ...process.env, OMC_ASK_ORIGINAL_TASK: prompt },
  });
  if (child.error) throw child.error;
  process.exitCode = child.status ?? 1;
}

// OMC teams take their leader from the caller's tmux pane, so OMC hosts the
// OpenCode orchestrator: outside tmux it re-enters through a session, and
// --standalone runs OpenCode's shell tool in this pane's process tree instead of
// the shared background service, whose tmux context is whatever started it.
export function openCodeInvocation(originalArgs, openCodeArgs, env, cwd, binary, self) {
  if (!env.TMUX) {
    const forwarded = ['OMC_WRAPPER_CONFIG_DIR', 'PATH'].filter(key => env[key]).flatMap(key => ['-e', `${key}=${env[key]}`]);
    return ['tmux', ['new-session', '-A', '-s', 'omc-opencode', '-c', cwd, ...forwarded, ...self, ...originalArgs]];
  }
  if (!binary) throw new Error('OpenCode was not found. Install it, then rerun install.sh.');
  return [binary, [...['--standalone', '--auto'].filter(flag => !openCodeArgs.includes(flag)), ...openCodeArgs]];
}

export function openCodeLaunchEnvironment(env, model) {
  if (!model || model === OPENCODE_NATIVE_DEFAULT) return env;
  // OpenCode 2's TUI has no --model flag. Its per-process config takes precedence
  // over the durable overlay without changing the user's next launch.
  return { ...env, OPENCODE_CONFIG_CONTENT: JSON.stringify({
    ...JSON.parse(env.OPENCODE_CONFIG_CONTENT || '{}'), model,
  }) };
}

function launchOpenCode(originalArgs, openCodeArgs, models) {
  let env = { ...process.env };
  let binary;
  if (env.TMUX) {
    const profile = createRunProfile({ ...selectedModels(), ...models });
    Object.assign(env, profile.env);
    env.OMC_ASK_ADVISOR_SCRIPT ||= fileURLToPath(new URL('./advisor.mjs', import.meta.url));
    for (const provider of PROVIDERS) {
      if (models[provider] !== undefined) env[`OMC_WRAPPER_${provider.toUpperCase()}_MODEL`] = models[provider];
    }
    binary = profile.run.native_binaries.opencode;
    env = openCodeLaunchEnvironment(env, profile.run.models.opencode);
  }
  const [command, args] = openCodeInvocation(originalArgs, openCodeArgs, env, process.cwd(), binary, [process.execPath, process.argv[1]]);
  const child = spawnSync(command, args, { stdio: 'inherit', env });
  if (child.error) throw child.error;
  process.exitCode = child.status ?? 1;
}

export async function main(originalArgs = process.argv.slice(2)) {
  const parsed = extractSelectors(originalArgs);
  const args = parsed.args;
  if (args[0] === 'provider') return providerCommand(args.slice(1));
  if (args[0] === 'opencode') return launchOpenCode(originalArgs, args.slice(1), parsed.models);
  if (args[0] === 'models') {
    const action = args[1] || 'show';
    if (action === 'show') {
      if (args.length > 2) throw new Error('Usage: omc models show');
      console.log(JSON.stringify({ config_file: modelConfigPath(), saved: savedModels(), selected: { ...selectedModels(), ...parsed.models } }, null, 2));
    } else if (action === 'set') {
      if (args.length !== 4 || !PROVIDERS.includes(args[2])) throw new Error('Usage: omc models set <claude|codex|opencode> MODEL');
      if (args[2] === 'opencode') return providerCommand(['model', args[3]]);
      const models = saveModels({ ...savedModels(), [args[2]]: selector(args[3]) });
      console.log(`${args[2]} model: ${models[args[2]]}`);
    } else if (action === 'list') {
      if (args.length > 2) throw new Error('Usage: omc models list');
      console.log('Local aliases and configured IDs; this is not an account entitlement list. Custom model IDs are accepted.');
      console.log(JSON.stringify(knownModels(), null, 2));
    } else if (action === 'select') {
      if (args.length > 2) throw new Error('Usage: omc models select');
      if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('Interactive selection needs a terminal. Use omc models set <provider> MODEL.');
      const current = { ...savedModels(), ...parsed.models };
      const input = createInterface({ input: process.stdin, output: process.stdout });
      try {
        for (const provider of PROVIDERS) {
          const answer = await input.question(`${provider} model [${current[provider]}]: `);
          if (answer) current[provider] = selector(answer);
        }
        console.log(JSON.stringify(saveModels(current), null, 2));
        await applyProviderConfig();
      } finally { input.close(); }
    } else throw new Error('Usage: omc models <show|set|select|list>');
    return;
  }
  if (args[0] === 'wrapper') {
    if (args[1] === 'status' && args.slice(2).every(arg => arg === '--json')) {
      const status = wrapperStatus();
      if (args.includes('--json')) console.log(JSON.stringify(status, null, 2));
      else {
        console.log(`OMC wrapper jobs${status.run_id ? ` for run ${status.run_id}` : ''}: ${status.jobs.length}`);
        for (const job of status.jobs) console.log(`${job.job_id || 'unknown'}  ${job.provider || 'unknown'}  ${job.provider_status?.outcome || job.status || job.outcome || 'unknown'}  ${job.provider_status?.actual_model || job.requested_model || job.model || 'model not exposed'}`);
        for (const error of status.errors) console.error(`${error.status_file}: ${error.error}`);
        if (status.truncated) console.log('Showing the 200 most recent job files.');
      }
      return;
    }
    if (!args[1] || args[1] === 'help' || args[1] === '--help') {
      console.log('omc wrapper status [--json]\nomc models <show|set|select|list>\nomc provider [help]\nomc opencode [OPENCODE ARGS]  OpenCode as the OMC orchestrator, in tmux\nPut --claude-model ID, --codex-model ID and --opencode-model ID before any OMC command.\nSelectors also work in the leading option block of launch and ask <claude|codex|opencode>.\nomc ask <claude|codex|opencode> [--model ID] [--effort LEVEL] [--agent-prompt ROLE] --prompt TEXT');
      return;
    }
    throw new Error('Usage: omc wrapper status [--json]');
  }
  const root = pluginRoot();
  // Managed installs use upstream's supported local plugin mechanism.
  if (isNativeLaunch(args) && installation().omc_root && !args.some(arg => arg === '--plugin-dir' || arg.startsWith('--plugin-dir='))) {
    const at = args[0] === 'launch' ? 1 : 0;
    args.splice(at, 0, '--plugin-dir', root);
  }
  process.env.OMC_ASK_ADVISOR_SCRIPT ||= fileURLToPath(new URL('./advisor.mjs', import.meta.url));
  const askingOpenCode = args[0] === 'ask' && args[1] === 'opencode';
  const recovery = await teamRecoveryProfile(root, args);
  if (recovery && Object.entries(parsed.models).some(([provider, model]) => model !== recovery.run.models[provider])) {
    throw new Error('Recovery retains the recorded worker model; change routes through a new assignment.');
  }
  const profile = recovery || createRunProfile({ ...selectedModels(), ...parsed.models });
  if (isNativeLaunch(args)) checkWrappedClaude(profile);
  Object.assign(process.env, profile.env);
  for (const provider of PROVIDERS) {
    if (parsed.models[provider] !== undefined) process.env[`OMC_WRAPPER_${provider.toUpperCase()}_MODEL`] = parsed.models[provider];
  }
  if (parsed.requestModel !== undefined) process.env.OMC_WRAPPER_REQUEST_MODEL = parsed.requestModel;
  if (parsed.effort !== undefined) process.env.OMC_WRAPPER_REQUEST_EFFORT = parsed.effort;
  if (isTeamStart(args)) {
    process.exitCode = await startTeamWithRecovery(root, args, process.env);
    return;
  }
  if (askingOpenCode) return askOpenCode(args.slice(2), root, fileURLToPath(new URL('./advisor.mjs', import.meta.url)));
  process.argv = [process.argv[0], process.argv[1], ...args];
  await import(pathToFileURL(join(root, 'bridge', 'cli.cjs')).href);
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`[omc] ${error.message}`);
    process.exitCode = 1;
  });
}
