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

const START_INVOKE_TIMEOUT_MS = 600000;
// Upstream brokers recovery through the persistent runtime owner and caps its wait at
// 300s (api-interop.js MIN/MAX_RECOVERY_TIMEOUT_MS). Fail fast just past that instead
// of blocking the wrapper for the full default budget on a wedged owner.
const RECOVERY_INVOKE_TIMEOUT_MS = 330000;
const PROBE_INVOKE_TIMEOUT_MS = 15000;

function invoke(root, args, env, cwd, { timeout = START_INVOKE_TIMEOUT_MS, quiet = false } = {}) {
  const result = spawnSync(process.execPath, [join(root, 'bridge/cli.cjs'), ...args], {
    cwd, env, encoding: 'utf8', timeout, maxBuffer: 16 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.stderr && !quiet) process.stderr.write(result.stderr);
  if (result.error) throw result.error;
  return result;
}

function jsonResult(output, accepts) {
  for (const line of output.trim().split('\n').reverse()) {
    try { const value = JSON.parse(line); if (accepts(value)) return value; } catch { /* diagnostic line */ }
  }
  throw new Error('OMC returned no structured team result; inspect its launch output before waiting.');
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function envInt(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

// A read-only local team state query (list-tasks, read-worker-heartbeat). It runs the
// bridge CLI, never a provider or model, so settle checks cost nothing on the quota.
function readEnvelope(root, operation, input, env, cwd) {
  try {
    const result = invoke(root, ['team', 'api', operation, '--input', JSON.stringify(input), '--json'],
      env, cwd, { timeout: PROBE_INVOKE_TIMEOUT_MS, quiet: true });
    return jsonResult(result.stdout, value => value?.operation === operation);
  } catch { return null; }
}

// A missed startup-evidence signal is usually a pane that was merely busy, not a dead
// worker. A live worker proves itself by claiming its task or writing a heartbeat; both
// are local reads. Brokered recover-worker costs a 180-300s owner round trip and, against
// a live worker, can only time out (recovery_request_timeout) while leaving a dangling
// request — the thrash we must avoid — so confirm genuine death before paying for it.
function workerStarted(root, teamName, worker, env, cwd, heartbeatMaxAgeMs) {
  const tasks = readEnvelope(root, 'list-tasks', { team_name: teamName }, env, cwd);
  if (tasks?.ok && Array.isArray(tasks.data?.tasks)
    && tasks.data.tasks.some(task => task?.owner === worker
      && ['in_progress', 'completed', 'failed'].includes(task?.status))) return true;
  const beat = readEnvelope(root, 'read-worker-heartbeat', { team_name: teamName, worker }, env, cwd);
  const heartbeat = beat?.ok ? beat.data?.heartbeat : null;
  if (heartbeat?.alive === true && typeof heartbeat.last_turn_at === 'string') {
    const age = Date.now() - Date.parse(heartbeat.last_turn_at);
    if (Number.isFinite(age) && age >= 0 && age <= heartbeatMaxAgeMs) return true;
  }
  return false;
}

// Poll the cheap liveness signals for a bounded grace window so a pane-busy worker has
// time to claim its task before we decide it is dead. Tunable for tests via env.
async function settleStartup(root, teamName, worker, env, cwd) {
  const window = envInt(env.OMC_TEAM_STARTUP_SETTLE_MS, 30000);
  const interval = envInt(env.OMC_TEAM_STARTUP_SETTLE_INTERVAL_MS, 1000);
  const heartbeatMaxAge = envInt(env.OMC_TEAM_HEARTBEAT_MAX_AGE_MS, 45000);
  const deadline = Date.now() + window;
  for (;;) {
    if (workerStarted(root, teamName, worker, env, cwd, heartbeatMaxAge)) return true;
    const left = deadline - Date.now();
    if (left <= 0) return false;
    await delay(Math.min(interval, left));
  }
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
    // Resolve a pane-busy false alarm with cheap local reads before the costly
    // brokered recovery; a live worker needs no replacement and would only time out.
    if (await settleStartup(root, started.teamName, failure.worker, env, cwd)) {
      startupRecovery.push({ worker: failure.worker, outcome: 'already_running', detectedBy: 'startup-settle' });
      continue;
    }
    // One immediate recovery attempt for this failed worker only. Other workers,
    // their leases and their edits remain owned by the existing team.
    try {
      const recoveryArgs = ['team', 'api', 'recover-worker', '--input', JSON.stringify({
        team_name: started.teamName, worker: failure.worker,
      }), '--json'];
      const profile = await teamRecoveryProfile(root, recoveryArgs, env, cwd);
      const result = invoke(root, recoveryArgs, profile.env, cwd, { timeout: RECOVERY_INVOKE_TIMEOUT_MS });
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
    for (const recovery of startupRecovery) console.log(`startup_recovery worker=${recovery.worker} outcome=${recovery.outcome}${recovery.detectedBy ? ` via=${recovery.detectedBy}` : ''}${recovery.error ? ` error=${recovery.error}` : ''}`);
    if (!ok) console.error('Do not enter the normal wait loop. Recover or reassign the failed worker task; keep healthy workers running and retain the current orchestrator.');
  }
  return ok ? 0 : 1;
}
