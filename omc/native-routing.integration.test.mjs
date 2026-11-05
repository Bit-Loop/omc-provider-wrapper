import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { pluginRoot } from './runtime.mjs';
import { createRunProfile } from './models.mjs';

const root = pluginRoot();
const { buildValidatedWorkerLaunchDescriptor, getWorkerEnv } = await import(pathToFileURL(join(root, 'dist/team/model-contract.js')).href);

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'omc-native-integration-'));
  const captured = join(dir, 'captured.json');
  const binary = join(dir, 'fixture-provider');
  writeFileSync(binary, `#!/usr/bin/python3
import json, os, sys, time
if '--version' in sys.argv:
    print('Claude Code native routing fixture 1.0')
    sys.exit(0)
if sys.argv[1:3] == ['auth', 'status']:
    print(json.dumps({'loggedIn': True}))
    sys.exit(0)
tty = [os.isatty(fd) for fd in (0,1,2)]
data = {'argv': sys.argv[1:], 'cwd': os.getcwd(), 'tty': tty, 'input': '' if tty[0] else sys.stdin.read()}
with open(os.environ['FIXTURE_CAPTURE'], 'w') as output:
    json.dump(data, output)
print(json.dumps({'type':'result','result':'NATIVE_ROUTING_FIXTURE_OK','is_error':False}), flush=True)
if tty[0]:
    time.sleep(1)
`, { mode: 0o700 });
  const env = { ...process.env, OMC_WRAPPER_STATE_DIR: join(dir, 'state'), FIXTURE_CAPTURE: captured };
  for (const name of ['OMC_WRAPPER_RUN_FILE', 'OMC_WRAPPER_RUN_ID', 'OMC_TEAM_WORKER', 'TMUX', 'TMUX_PANE']) delete env[name];
  const profile = createRunProfile({ claude: 'claude-selected-fixture', codex: 'codex-selected-fixture' }, env,
    { cwd: dir, nativeBinaries: { claude: binary, codex: binary } });
  profile.run.native_binaries = { claude: binary, codex: binary };
  writeFileSync(profile.filename, JSON.stringify(profile.run), { mode: 0o600 });
  t.after(() => { rmSync(profile.run.provider_path, { recursive: true, force: true }); rmSync(dir, { recursive: true, force: true }); });
  return { dir, captured, profile };
}

async function execute(command, args, options) {
  const child = spawn(command, args, { ...options, stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', data => { stdout += data; });
  child.stderr.on('data', data => { stderr += data; });
  child.stdin.end('native-stdin-fixture');
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  return { code, stdout, stderr };
}

function jobOf(profile) {
  const jobs = join(profile.run.state_dir, 'runs', profile.run.run_id, 'jobs');
  const files = readdirSync(jobs).filter(file => file.endsWith('.json'));
  assert.equal(files.length, 1);
  return JSON.parse(readFileSync(join(jobs, files[0]), 'utf8'));
}

test('installed OMC launch reaches wrapper native mode with selected model, cwd and stdin', async t => {
  const { dir, captured, profile } = fixture(t);
  const result = await execute(process.execPath, [join(root, 'bridge/cli.cjs'), 'launch', '--yolo', '--model', 'claude-explicit-fixture', '-p', 'fixture prompt'], { cwd: dir, env: profile.env });
  assert.equal(result.code, 0, result.stderr);
  const invocation = JSON.parse(readFileSync(captured, 'utf8'));
  assert.equal(invocation.cwd, dir);
  assert.equal(invocation.input, 'native-stdin-fixture');
  assert.ok(invocation.argv.includes('--dangerously-skip-permissions'));
  assert.equal(invocation.argv[invocation.argv.indexOf('--model') + 1], 'claude-explicit-fixture');
  const job = jobOf(profile);
  assert.equal(job.provider, 'claude');
  assert.equal(job.requested_model, 'claude-explicit-fixture');
  assert.equal(job.job_state, 'exited');
  assert.match(result.stdout, /NATIVE_ROUTING_FIXTURE_OK/);
});

for (const provider of ['claude', 'codex']) {
  test(`installed OMC ${provider} worker descriptor uses pinned wrapper after environment filtering`, async t => {
    const { dir, captured, profile } = fixture(t);
    const descriptor = buildValidatedWorkerLaunchDescriptor(provider, { teamName: 'wrapper-fixture', workerName: 'worker-1',
      model: 'omc-generated-role-model', resolvedBinaryPath: join(profile.run.provider_path, provider) });
    const filtered = getWorkerEnv('wrapper-fixture', 'worker-1', provider, profile.env);
    assert.equal(filtered.OMC_WRAPPER_RUN_FILE, undefined);
    const result = await execute(descriptor.binary, descriptor.args, { cwd: dir,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, FIXTURE_CAPTURE: captured, ...filtered } });
    assert.equal(result.code, 0, result.stderr);
    const invocation = JSON.parse(readFileSync(captured, 'utf8'));
    assert.equal(invocation.argv[invocation.argv.indexOf('--model') + 1], `${provider}-selected-fixture`);
    const job = jobOf(profile);
    assert.equal(job.run_id, profile.run.run_id);
    assert.equal(job.provider, provider);
    assert.equal(job.job_state, 'exited');
  });
}

test('fresh private tmux OMC launch preserves the wrapped native terminal', async t => {
  const { dir, captured, profile } = fixture(t);
  const tmuxDir = join(dir, 'tmux');
  mkdirSync(tmuxDir, { mode: 0o700 });
  const socket = join(tmuxDir, `tmux-${process.getuid()}`, 'default');
  t.after(() => spawnSync('/usr/bin/tmux', ['-S', socket, 'kill-server'], { stdio: 'ignore' }));
  const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
  const command = [process.execPath, join(root, 'bridge/cli.cjs'), 'launch', '--yolo'].map(quote).join(' ');
  const result = await execute('/usr/bin/script', ['-qec', command, '/dev/null'], {
    cwd: dir, env: { ...profile.env, TMUX_TMPDIR: tmuxDir, TERM: 'xterm-256color' }, timeout: 15000,
  });
  const invocation = JSON.parse(readFileSync(captured, 'utf8'));
  assert.deepEqual(invocation.tty, [true, true, true]);
  assert.ok(invocation.argv.includes('--dangerously-skip-permissions'));
  assert.equal(invocation.argv[invocation.argv.indexOf('--model') + 1], 'claude-selected-fixture');
  const job = jobOf(profile);
  assert.equal(job.provider, 'claude');
  assert.equal(job.job_state, 'exited');
  assert.equal(job.exit_code, 0, result.stderr);
});
