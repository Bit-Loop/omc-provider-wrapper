import { lstatSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { delimiter, dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { installation } from './installation.mjs';
import { nativeStateRoot, readRunProfile } from './models.mjs';

const safeName = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const teamCommands = new Set(['api', 'status', 'shutdown', 'help', '--help', '-h', 'wait', 'list', 'ls', 'resume', 'logs', 'attach']);

export function isTeamStart(args) {
  return args[0] === 'team' && args.length > 1 && !teamCommands.has(args[1]) && !args.includes('--help') && !args.includes('-h');
}

function recoveryInput(args) {
  if (args[0] !== 'team' || args[1] !== 'api' || args[2] !== 'recover-worker' || args.includes('--help')) return null;
  const index = args.indexOf('--input');
  if (index < 0 || !args[index + 1]) return null; // upstream retains argument diagnostics
  return JSON.parse(args[index + 1]);
}

// Recovery must retain the launch-time shim path, run profile and provider route.
// A new command profile creates a different shim path even for the same provider;
// upstream correctly rejects that mismatch against its persisted launch descriptor.
export async function teamRecoveryProfile(root, args, env = process.env, cwd = process.cwd()) {
  const input = recoveryInput(args);
  if (!input) return null;
  const team = input.team_name, worker = input.worker;
  if (typeof team !== 'string' || typeof worker !== 'string' || !safeName.test(team) || !safeName.test(worker)) {
    throw new Error('Recovery requires a valid team_name and worker.');
  }
  const { canonicalTeamStateRoot } = await import(pathToFileURL(join(root, 'dist/team/state-paths.js')).href);
  const work = input.workingDirectory ? resolve(input.workingDirectory) : cwd;
  const config = JSON.parse(readFileSync(join(canonicalTeamStateRoot(work, team), 'config.json'), 'utf8'));
  const descriptor = config.workers?.find(item => item.name === worker)?.launch_descriptor;
  if (!descriptor?.binary || !['claude', 'codex'].includes(descriptor.provider)) {
    throw new Error('Recorded worker has no supported wrapper launch descriptor; keep its task for reassignment.');
  }
  const directory = dirname(descriptor.binary);
  const launches = realpathSync(join(nativeStateRoot(env), 'launches'));
  const rel = relative(launches, realpathSync(directory));
  if (!rel || rel.startsWith('..' + sep) || rel === '..' || rel.includes(sep)) {
    throw new Error('Recorded recovery profile is outside the private wrapper launch directory.');
  }
  const filename = join(directory, 'run.json');
  const metadata = lstatSync(filename);
  if (!metadata.isFile() || metadata.uid !== process.getuid() || (metadata.mode & 0o077) !== 0) {
    throw new Error('Recovery requires an owner-only private wrapper profile.');
  }
  const run = readRunProfile({ OMC_WRAPPER_RUN_FILE: filename });
  const shim = realpathSync(descriptor.binary);
  const installedRoot = installation(env).root;
  const trustedShims = [fileURLToPath(new URL('./provider-cli.sh', import.meta.url)),
    ...(installedRoot ? [join(installedRoot, 'omc/provider-cli.sh')] : [])];
  if (resolve(run.provider_path) !== resolve(directory)
    || resolve(descriptor.binary) !== join(resolve(directory), descriptor.provider)
    || realpathSync(run.workspace) !== realpathSync(config.leader_cwd || work)
    || !trustedShims.some(candidate => { try { return realpathSync(candidate) === shim; } catch { return false; } })
    || (statSync(descriptor.binary).mode & 0o111) === 0) {
    throw new Error('Recorded wrapper recovery profile does not match the worker launch.');
  }
  return { run, filename, env: { ...env, PATH: [directory, env.PATH || ''].join(delimiter),
    OMC_WRAPPER_RUN_FILE: filename, OMC_WRAPPER_RUN_ID: run.run_id, OMC_WRAPPER_STATE_DIR: run.state_dir } };
}

function invoke(root, args, env, cwd) {
  const result = spawnSync(process.execPath, [join(root, 'bridge/cli.cjs'), ...args], {
    cwd, env, encoding: 'utf8', timeout: 600000, maxBuffer: 16 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.stderr) process.stderr.write(result.stderr);
  if (result.error) throw result.error;
  return result;
}

function jsonResult(output, accepts) {
  for (const line of output.trim().split('\n').reverse()) {
    try { const value = JSON.parse(line); if (accepts(value)) return value; } catch { /* diagnostic line */ }
  }
  throw new Error('OMC returned no structured team result; inspect its launch output before waiting.');
}

export async function startTeamWithRecovery(root, args, env = process.env, cwd = process.cwd()) {
  const json = args.includes('--json');
  const initial = invoke(root, json ? args : [...args, '--json'], env, cwd);
  let started;
  try {
    started = jsonResult(initial.stdout, value => typeof value?.teamName === 'string' && Array.isArray(value.startupFailures));
  } catch (error) {
    if (initial.stdout) process.stdout.write(initial.stdout);
    throw error;
  }
  const startupRecovery = [];
  const remaining = [];
  for (const failure of started.startupFailures) {
    if (typeof failure.worker !== 'string' || !safeName.test(failure.worker)) {
      remaining.push(failure);
      continue;
    }
    // One immediate recovery attempt for this failed worker only. Other workers,
    // their leases and their edits remain owned by the existing team.
    try {
      const recoveryArgs = ['team', 'api', 'recover-worker', '--input', JSON.stringify({
        team_name: started.teamName, worker: failure.worker,
      }), '--json'];
      const profile = await teamRecoveryProfile(root, recoveryArgs, env, cwd);
      const result = invoke(root, recoveryArgs, profile.env, cwd);
      const envelope = jsonResult(result.stdout, value => value?.operation === 'recover-worker');
      const outcome = envelope.ok === true ? envelope.data?.result : null;
      const recovered = outcome?.committed === true
        && ['already_running', 'recovered'].includes(outcome.outcome);
      startupRecovery.push({ worker: failure.worker, outcome: recovered ? outcome.outcome : 'failed',
        ...(!recovered ? { error: outcome?.error || envelope.error?.code || 'recovery_unconfirmed' } : {}) });
      if (!recovered) remaining.push(failure);
    } catch {
      startupRecovery.push({ worker: failure.worker, outcome: 'failed', error: 'recovery_unconfirmed' });
      remaining.push(failure);
    }
  }
  const ok = remaining.length === 0
    && ((started.ok === true && initial.status === 0) || started.startupFailures.length > 0);
  const output = { ...started, ok, startupFailures: remaining, startupRecovery,
    waitAllowed: ok, nextAction: ok ? 'wait-for-workers' : 'recover-or-reassign' };
  if (json) console.log(JSON.stringify(output));
  else {
    console.log(`${ok ? 'Team started' : 'Team start needs recovery'}: ${started.teamName}`);
    console.log(`tmux session: ${started.sessionName}`);
    for (const recovery of startupRecovery) console.log(`startup_recovery worker=${recovery.worker} outcome=${recovery.outcome}${recovery.error ? ` error=${recovery.error}` : ''}`);
    if (!ok) console.error('Do not enter the normal wait loop. Recover or reassign the failed worker task; keep healthy workers running. GLM remains the lead.');
  }
  return ok ? 0 : 1;
}
