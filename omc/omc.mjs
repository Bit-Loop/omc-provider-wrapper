#!/usr/bin/env node
import { join } from 'node:path';
import { realpathSync } from 'node:fs';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { pluginRoot } from './runtime.mjs';
import { checkWrappedClaude, isNativeLaunch } from './launch-preflight.mjs';
import { installation } from './installation.mjs';
import { PROVIDERS, modelConfigPath, savedModels, selectedModels, saveModels, knownModels,
  extractSelectors, createRunProfile, wrapperStatus, selector } from './models.mjs';

export async function main(originalArgs = process.argv.slice(2)) {
  const parsed = extractSelectors(originalArgs);
  const args = parsed.args;
  if (args[0] === 'models') {
    const action = args[1] || 'show';
    if (action === 'show') {
      if (args.length > 2) throw new Error('Usage: omc models show');
      console.log(JSON.stringify({ config_file: modelConfigPath(), saved: savedModels(), selected: { ...selectedModels(), ...parsed.models } }, null, 2));
    } else if (action === 'set') {
      if (args.length !== 4 || !PROVIDERS.includes(args[2])) throw new Error('Usage: omc models set <claude|codex> MODEL');
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
      console.log('omc wrapper status [--json]\nomc models <show|set|select|list>\nPut --claude-model ID and --codex-model ID before any OMC command.\nSelectors also work in the leading option block of launch and ask <claude|codex>.\nomc ask <claude|codex> [--model ID] [--effort LEVEL] [--agent-prompt ROLE] --prompt TEXT');
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
  const profile = createRunProfile({ ...selectedModels(), ...parsed.models });
  if (isNativeLaunch(args)) checkWrappedClaude(profile);
  Object.assign(process.env, profile.env);
  for (const provider of PROVIDERS) {
    if (parsed.models[provider] !== undefined) process.env[`OMC_WRAPPER_${provider.toUpperCase()}_MODEL`] = parsed.models[provider];
  }
  if (parsed.requestModel !== undefined) process.env.OMC_WRAPPER_REQUEST_MODEL = parsed.requestModel;
  if (parsed.effort !== undefined) process.env.OMC_WRAPPER_REQUEST_EFFORT = parsed.effort;
  process.argv = [process.argv[0], process.argv[1], ...args];
  await import(pathToFileURL(join(root, 'bridge', 'cli.cjs')).href);
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`[omc] ${error.message}`);
    process.exitCode = 1;
  });
}
