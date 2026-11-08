import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRunProfile } from './models.mjs';
import { isTeamStart } from './team-recovery.mjs';
import { teamRecoveryProfile } from './team-recovery.mjs';
import { pluginRoot } from './runtime.mjs';

const frontend = process.env.OMC_RECOVERY_TEST_FRONTEND || fileURLToPath(new URL('./omc.mjs', import.meta.url));

function lab(t, scenario = 'recover', base = tmpdir()) {
  const home = mkdtempSync(join(base, '.omc-team-recovery-'));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const cwd = join(home, 'work'), plugin = join(home, 'plugin'), config = join(home, 'config');
  for (const dir of [cwd, config, join(plugin, 'bridge'), join(plugin, 'dist/team')]) mkdirSync(dir, { recursive: true });
  assert.equal(spawnSync('git', ['init', '-q'], { cwd }).status, 0);
  writeFileSync(join(plugin, 'package.json'), '{"type":"module"}');
  writeFileSync(join(plugin, 'dist/team/state-paths.js'), `import { join } from 'node:path'; export const canonicalTeamStateRoot = (cwd,team) => join(cwd,'.omc/state/team',team);`);
  const native = join(home, 'native');
  writeFileSync(native, '#!/bin/sh\nexit 0\n', { mode: 0o700 });
  writeFileSync(join(config, 'install.json'), JSON.stringify({ root: fileURLToPath(new URL('../', import.meta.url)),
    native_binaries: { claude: native, codex: native } }));
  const events = join(home, 'events.jsonl');
  const env = { ...process.env, HOME: home, XDG_STATE_HOME: join(home, 'state'),
    OMC_WRAPPER_CONFIG_DIR: config, OMC_WRAPPER_PLUGIN_ROOT: plugin,
    OMC_WRAPPER_STATE_DIR: join(home, 'native-state'), FIXTURE_EVENTS: events,
    FIXTURE_SCENARIO: scenario,
    // Keep the startup settle window tiny so dead-worker scenarios probe once and recover
    // within the test timeout; the real default is 30s.
    OMC_TEAM_STARTUP_SETTLE_MS: '1', OMC_TEAM_STARTUP_SETTLE_INTERVAL_MS: '10' };
  for (const key of ['OMC_WRAPPER_RUN_FILE', 'OMC_WRAPPER_RUN_ID', 'OMC_WRAPPER_CLAUDE_MODEL', 'OMC_WRAPPER_CODEX_MODEL', 'CLAUDECODE']) delete env[key];
  const original = createRunProfile({ claude: 'original-claude', codex: 'original-codex' }, env,
    { cwd, home, nativeBinaries: { claude: native, codex: native } });
  const teamDir = join(cwd, '.omc/state/team/fixture-team');
  mkdirSync(teamDir, { recursive: true });
  const team = { name: 'fixture-team', leader_cwd: cwd, workers: [
    { name: 'worker-1', worker_cli: 'claude', assigned_tasks: [], launch_descriptor: { schema_version: 1,
      provider: 'claude', model: 'original-claude', binary: join(original.run.provider_path, 'claude'), args: [] } },
    { name: 'worker-2', worker_cli: 'claude', assigned_tasks: ['2'] },
  ] };
  writeFileSync(join(teamDir, 'config.json'), JSON.stringify(team));
  writeFileSync(join(plugin, 'bridge/cli.cjs'), `
const fs=require('node:fs'),path=require('node:path');
const args=process.argv.slice(2), action=args[0]==='team'&&args[1]==='api'?args[2]:'start';
const config=JSON.parse(fs.readFileSync(path.join(process.cwd(),'.omc/state/team/fixture-team/config.json')));
const expected=path.dirname(config.workers[0].launch_descriptor.binary);
const actual=process.env.PATH.split(path.delimiter)[0];
fs.appendFileSync(process.env.FIXTURE_EVENTS,JSON.stringify({action,args,profile:process.env.OMC_WRAPPER_RUN_FILE,actual,expected})+'\\n');
if(action==='list-tasks'){
 const alive=process.env.FIXTURE_SCENARIO==='settle';
 const tasks=alive?[{id:'2',status:'in_progress',owner:'worker-1'}]:[{id:'2',status:'pending',owner:null}];
 console.log(JSON.stringify({ok:true,operation:'list-tasks',data:{count:tasks.length,tasks}}));
 process.exit(0);
}
if(action==='read-worker-heartbeat'){
 const worker=JSON.parse(args[args.indexOf('--input')+1]).worker;
 console.log(JSON.stringify({ok:true,operation:'read-worker-heartbeat',data:{worker,heartbeat:null}}));
 process.exit(0);
}
if(action==='recover-worker'){
 const payload=JSON.parse(args[args.indexOf('--input')+1]);
 const success=actual===expected&&payload.worker==='worker-1'&&process.env.FIXTURE_SCENARIO!=='unrecoverable';
 console.log(JSON.stringify({ok:true,operation:action,data:{result:success?{outcome:'recovered',committed:true,workerName:'worker-1'}:{outcome:'failed',committed:false,error:'launch_descriptor_unresolvable'}}}));
 process.exit(success?0:1);
}
const healthy=process.env.FIXTURE_SCENARIO==='healthy';
console.log(JSON.stringify({teamName:'fixture-team',sessionName:'fixture',instanceId:'fixture-instance',workerCount:2,agentType:'claude',ok:healthy,startupFailures:healthy?[]:[{worker:'worker-1',reason:'worker_startup_evidence_missing_pane_busy'}]}));
process.exit(healthy?0:1);
`);
  const run = args => spawnSync(process.execPath, [frontend, ...args], { cwd, env, encoding: 'utf8', timeout: 10000 });
  return { home, cwd, env, original, teamDir, run, events: () => readFileSync(events, 'utf8').trim().split('\n').map(line => JSON.parse(line)) };
}

test('recovery reuses the recorded wrapper profile instead of a fresh command profile', t => {
  const fixture = lab(t);
  const result = fixture.run(['team', 'api', 'recover-worker', '--input', JSON.stringify({ team_name: 'fixture-team', worker: 'worker-1' }), '--json']);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(JSON.parse(result.stdout).data.result.outcome, 'recovered');
  assert.equal(fixture.events()[0].profile, fixture.original.filename);
});

test('recorded profile satisfies the real pinned OMC provider-path recovery check', async t => {
  // OMC deliberately refuses executable paths under /tmp, even for fixtures.
  // Use an owner-private disposable home without weakening its path checks.
  const fixture = lab(t, 'recover', homedir());
  const fresh = createRunProfile({ claude: 'new-default', codex: 'new-default' }, fixture.env,
    { cwd: fixture.cwd, home: fixture.home, nativeBinaries: fixture.original.run.native_binaries });
  const root = pluginRoot();
  const args = ['team', 'api', 'recover-worker', '--input', JSON.stringify({ team_name: 'fixture-team', worker: 'worker-1' }), '--json'];
  const restored = await teamRecoveryProfile(root, args, fixture.env, fixture.cwd);
  const resolver = `import {resolveValidatedBinaryPath} from ${JSON.stringify(new URL('dist/team/model-contract.js', 'file://' + root + '/').href)}; console.log(resolveValidatedBinaryPath('claude'));`;
  const resolveProvider = env => {
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', resolver], { env, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  const recorded = join(fixture.original.run.provider_path, 'claude');
  assert.notEqual(resolveProvider(fresh.env), recorded, 'a fresh command profile reproduces the old OMC path mismatch');
  assert.equal(resolveProvider(restored.env), recorded, 'restored profile passes the actual OMC path identity check');
  assert.equal(restored.run.models.claude, 'original-claude');
});

test('only team launch commands enter automatic startup recovery', () => {
  for (const args of [['team', '1:claude:executor', 'task'], ['team', '--task', 'task']]) assert.equal(isTeamStart(args), true);
  for (const args of [['team'], ['team', 'status', 'x'], ['team', 'api', 'recover-worker'], ['team', '--help'], ['opencode']]) assert.equal(isTeamStart(args), false);
});

test('a failed startup is recovered immediately and never enters the normal wait path', t => {
  const fixture = lab(t);
  const result = fixture.run(['team', '2:claude:executor', '--json', '1. First task\n2. Second task']);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.ok, true);
  assert.deepEqual(output.startupFailures, []);
  assert.equal(output.startupRecovery[0].outcome, 'recovered');
  const actions = fixture.events().map(event => event.action);
  assert.deepEqual(actions.filter(action => action === 'start' || action === 'recover-worker'), ['start', 'recover-worker']);
  assert.equal(fixture.events().find(event => event.action === 'recover-worker').profile, fixture.original.filename);
});

test('a pane-busy false alarm settles via local reads instead of brokered recovery', t => {
  const fixture = lab(t, 'settle');
  const result = fixture.run(['team', '2:claude:executor', '--json', '1. First task\n2. Second task']);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.ok, true);
  assert.equal(output.waitAllowed, true);
  assert.deepEqual(output.startupFailures, []);
  assert.equal(output.startupRecovery[0].outcome, 'already_running');
  assert.equal(output.startupRecovery[0].detectedBy, 'startup-settle');
  const actions = fixture.events().map(event => event.action);
  assert.ok(actions.includes('list-tasks'));
  assert.ok(!actions.includes('recover-worker'), 'a live worker must not pay for brokered recovery');
});

test('unrecoverable startup preserves the other worker and directs reassignment rather than waiting', t => {
  const fixture = lab(t, 'unrecoverable');
  const result = fixture.run(['team', '2:claude:executor', '--json', '1. First task\n2. Second task']);
  assert.notEqual(result.status, 0);
  const output = JSON.parse(result.stdout);
  assert.equal(output.ok, false);
  assert.equal(output.waitAllowed, false);
  assert.equal(output.nextAction, 'recover-or-reassign');
  const actions = fixture.events().map(event => event.action);
  assert.deepEqual(actions.filter(action => action === 'start' || action === 'recover-worker'), ['start', 'recover-worker']);
  assert.deepEqual(JSON.parse(readFileSync(join(fixture.teamDir, 'config.json'))).workers[1].assigned_tasks, ['2']);
});

test('healthy starts do not recover or replace workers', t => {
  const fixture = lab(t, 'healthy');
  const result = fixture.run(['team', '2:claude:executor', '--json', '1. First task\n2. Second task']);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).ok, true);
  assert.deepEqual(fixture.events().map(event => event.action), ['start']);
});

test('recovery rejects a recorded profile that is writable by other users', t => {
  const fixture = lab(t);
  const profile = JSON.parse(readFileSync(fixture.original.filename));
  writeFileSync(fixture.original.filename, JSON.stringify(profile), { mode: 0o600 });
  // writeFile preserves an existing mode; chmod deliberately simulates a damaged profile.
  spawnSync('chmod', ['666', fixture.original.filename]);
  const result = fixture.run(['team', 'api', 'recover-worker', '--input', JSON.stringify({ team_name: 'fixture-team', worker: 'worker-1' }), '--json']);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /private.*profile|profile.*private/i);
});
