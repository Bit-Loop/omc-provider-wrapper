import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, statSync, readdirSync, readlinkSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { selector, savedModels, saveModels, selectedModels, requestSelection, knownModels,
  extractSelectors, createRunProfile, wrapperStatus } from './models.mjs';
import { wrapperRequest } from './advisor.mjs';
import { pluginRoot } from './runtime.mjs';

const directory = mkdtempSync(join(tmpdir(), 'omc-routing-'));
const home = join(directory, 'home');
mkdirSync(join(home, '.claude'), { recursive: true });
mkdirSync(join(home, '.codex'), { recursive: true });
writeFileSync(join(home, '.claude/settings.json'), JSON.stringify({ model: 'claude-local-fixture',
  modelSettings: { 'claude-opus-5-5': {}, 'claude-opus-4-8': {} } }));
writeFileSync(join(home, '.codex/config.toml'), 'model = "codex-local-fixture"\n[profiles.review]\nmodel = "codex-review-fixture"\n');
mkdirSync(join(home, '.codex/prompts'));
writeFileSync(join(home, '.codex/prompts/critic.md'), readFileSync(join(pluginRoot(), 'agents/critic.md')));
const configFile = join(directory, 'config/models.json');
const env = { OMC_WRAPPER_MODEL_CONFIG: configFile };
const launcher = fileURLToPath(new URL('./omc.mjs', import.meta.url));
test.after(() => rmSync(directory, { recursive: true, force: true }));

test('model defaults come from native settings; custom IDs persist exactly with private permissions', () => {
  assert.deepEqual(savedModels(env, home), { claude: 'claude-local-fixture', codex: 'codex-local-fixture' });
  const models = { claude: 'custom/claude@preview', codex: ' vendor/model exact ' };
  assert.deepEqual(saveModels(models, env, home), models);
  assert.deepEqual(savedModels(env, home), models);
  assert.equal(statSync(configFile).mode & 0o777, 0o600);
  assert.equal(statSync(join(directory, 'config')).mode & 0o777, 0o700);
  for (const value of ['', ' ', 'line\nbreak', 'line\rbreak', 'nul\0byte']) assert.throws(() => selector(value));
  assert.equal(selector('--custom-model'), '--custom-model');
});

test('active Claude and Codex config roots supply defaults and listed IDs with tilde expansion', () => {
  const claudeRoot = join(home, 'active-claude');
  const codexRoot = join(home, 'active-codex');
  mkdirSync(claudeRoot);
  mkdirSync(codexRoot);
  writeFileSync(join(claudeRoot, 'settings.json'), JSON.stringify({ model: 'active-claude-default', modelSettings: { 'active-claude-configured': {} } }));
  writeFileSync(join(codexRoot, 'config.toml'), 'model = "active-codex-default"\n[profiles.review]\nmodel = "active-codex-profile"\n');
  const active = { OMC_WRAPPER_MODEL_CONFIG: join(directory, 'active-models.json'),
    CLAUDE_CONFIG_DIR: '~/active-claude', CODEX_HOME: '~/active-codex' };
  const expected = { claude: 'active-claude-default', codex: 'active-codex-default' };
  assert.deepEqual(savedModels(active, home), expected);
  assert.deepEqual(selectedModels(active, home), expected);
  assert.deepEqual(selectedModels({ ...active, CLAUDE_CONFIG_DIR: claudeRoot, CODEX_HOME: codexRoot }, home), expected);
  const known = knownModels(active, home);
  assert.ok(known.claude.includes('active-claude-configured'));
  assert.ok(known.codex.includes('active-codex-default'));
  assert.ok(known.codex.includes('active-codex-profile'));
  assert.ok(!known.claude.includes('claude-opus-5-5'));
  assert.ok(!known.codex.includes('codex-local-fixture'));
  assert.equal(selectedModels(active, home).codex, 'active-codex-default', 'profile model must not replace the root default');
});

test('request selectors take precedence over provider env, run profile and saved defaults', () => {
  const runId = randomUUID();
  const runFile = join(directory, 'precedence.json');
  writeFileSync(runFile, JSON.stringify({ schema_version: 1, run_id: runId, models: { claude: 'run-claude', codex: 'run-codex' } }));
  const scoped = { ...env, OMC_WRAPPER_RUN_FILE: runFile };
  assert.equal(requestSelection('codex', scoped, home).model, 'run-codex');
  const override = { ...scoped, OMC_WRAPPER_CODEX_MODEL: 'env-codex' };
  assert.equal(selectedModels(override, home).codex, 'env-codex');
  assert.deepEqual(requestSelection('codex', { ...override, OMC_WRAPPER_REQUEST_MODEL: 'call-codex', OMC_WRAPPER_REQUEST_EFFORT: 'ultra' }, home),
    { model: 'call-codex', effort: 'ultra' });
  assert.ok(knownModels(env, home).codex.includes('codex-review-fixture'));
});

test('explicit native model environment is preserved below wrapper overrides and inherited profiles', () => {
  const native = { ...env, ANTHROPIC_MODEL: 'native-anthropic', CLAUDE_MODEL: 'native-claude',
    OMC_CODEX_DEFAULT_MODEL: 'native-codex', OMC_EXTERNAL_MODELS_DEFAULT_CODEX_MODEL: 'native-external-codex', OMC_MODEL_HIGH: 'tier-only' };
  assert.deepEqual(selectedModels(native, home), { claude: 'native-anthropic', codex: 'native-codex' });
  assert.deepEqual(selectedModels({ ...env, CLAUDE_MODEL: 'native-claude', OMC_EXTERNAL_MODELS_DEFAULT_CODEX_MODEL: 'native-external-codex' }, home),
    { claude: 'native-claude', codex: 'native-external-codex' });
  assert.deepEqual(selectedModels({ ...env, OMC_MODEL_HIGH: 'tier-only' }, home), savedModels(env, home));
  const filename = join(directory, 'native-env-profile.json');
  writeFileSync(filename, JSON.stringify({ schema_version: 1, run_id: randomUUID(), models: { claude: 'inherited-claude', codex: 'inherited-codex' } }));
  const inherited = { ...native, OMC_WRAPPER_RUN_FILE: filename };
  assert.deepEqual(selectedModels(inherited, home), { claude: 'inherited-claude', codex: 'inherited-codex' });
  assert.deepEqual(selectedModels({ ...inherited, OMC_WRAPPER_CLAUDE_MODEL: 'wrapper-claude', OMC_WRAPPER_CODEX_MODEL: 'wrapper-codex' }, home),
    { claude: 'wrapper-claude', codex: 'wrapper-codex' });
});

test('local Claude modelSettings IDs are listed with aliases and exact custom selections', () => {
  const local = { OMC_WRAPPER_MODEL_CONFIG: join(directory, 'list-models.json'), OMC_WRAPPER_CLAUDE_MODEL: ' selected/custom-id ' };
  saveModels({ claude: 'saved/custom-id', codex: 'saved-codex' }, local, home);
  const models = knownModels(local, home);
  for (const id of ['opus', 'sonnet', 'haiku', 'claude-opus-5-5', 'claude-opus-4-8', 'saved/custom-id', ' selected/custom-id ']) {
    assert.ok(models.claude.includes(id), `Missing local model ${id}`);
  }
});

test('advisor selector parsing preserves role options and literal prompt flags', () => {
  const parsed = extractSelectors(['--claude-model', 'orchestrator', 'ask', 'codex', '--model=advisor', '--effort', 'xhigh', '--agent-prompt', 'critic', '--prompt', '--model', 'literal', '--codex-model=literal-too']);
  assert.deepEqual(parsed.args, ['ask', 'codex', '--agent-prompt', 'critic', '--prompt', '--model', 'literal', '--codex-model=literal-too']);
  assert.deepEqual(parsed.models, { claude: 'orchestrator' });
  assert.equal(parsed.requestModel, 'advisor');
  assert.equal(parsed.effort, 'xhigh');
  assert.deepEqual(extractSelectors(['ask', 'claude', 'explain', '--model', 'literal']).args, ['ask', 'claude', 'explain', '--model', 'literal']);
  assert.deepEqual(extractSelectors(['ask', 'claude', '--', '--effort', 'literal']).args, ['ask', 'claude', '--prompt', '--effort', 'literal']);
  assert.deepEqual(extractSelectors(['models', 'set', 'claude', '--claude-model=literal']).args, ['models', 'set', 'claude', '--claude-model=literal']);
  assert.deepEqual(extractSelectors(['--claude-model', 'override', 'models', 'set', 'claude', '--codex-model=literal']).args, ['models', 'set', 'claude', '--codex-model=literal']);
  assert.equal(extractSelectors(['launch', '--model', 'native', '--prompt', 'ask']).models.claude, 'native');
  assert.throws(() => extractSelectors(['ask', 'codex', '--model', '--prompt', 'hello']), /Missing value/);
});

test('native launch model flag is preserved and sets Claude run model', () => {
  const args = ['launch', '--model', 'native-model', '--codex-model', 'worker-model', '--permission-mode', 'default'];
  const parsed = extractSelectors(args);
  assert.deepEqual(parsed.args, ['launch', '--model', 'native-model', '--permission-mode', 'default']);
  assert.deepEqual(parsed.models, { codex: 'worker-model', claude: 'native-model' });
  assert.equal(extractSelectors(['-m', 'short-native']).models.claude, 'short-native');
  for (const flag of ['--yolo', '--madmax']) {
    const args = ['launch', flag, '--codex-model', 'worker', '--append-system-prompt', '--claude-model=literal'];
    const parsed = extractSelectors(args);
    assert.deepEqual(parsed.args, ['launch', flag, '--append-system-prompt', '--claude-model=literal']);
    assert.deepEqual(parsed.models, { codex: 'worker' });
  }
});

test('upstream goal, brief and native option operands stay byte-for-byte unchanged', () => {
  const examples = [
    ['ultragoal', 'add-goal', '--title', 'fixture', '--objective', '--codex-model=literal'],
    ['launch', '--append-system-prompt', '--claude-model=literal'],
    ['launch', '--append-system-prompt', '--model=literal'],
    ['launch', '--append-system-prompt', '--model', 'literal'],
    ['team', 'brief', '--objective', '--claude-model=literal', '--title', 'literal'],
    ['ultragoal', 'add-goal', '--objective', '--codex-model', 'literal'],
  ];
  for (const args of examples) {
    const parsed = extractSelectors(args);
    assert.deepEqual(parsed.args, args);
    assert.deepEqual(parsed.models, {});
  }
  const objective = ['ultragoal', 'add-goal', '--objective', '--codex-model=literal'];
  const scoped = extractSelectors(['--codex-model', 'worker-fixture', ...objective]);
  assert.deepEqual(scoped.args, objective);
  assert.deepEqual(scoped.models, { codex: 'worker-fixture' });
});

test('private scoped provider paths preserve worker routing after custom environment is dropped', () => {
  const shim = join(directory, 'provider-fixture.sh');
  writeFileSync(shim, '#!/bin/sh\nprintf "%s\\n" "$0" "$@"\n', { mode: 0o700 });
  const runId = randomUUID();
  const profile = createRunProfile({ claude: 'claude-fixture', codex: 'codex-fixture' }, { OMC_WRAPPER_RUN_ID: runId, PATH: '/usr/bin' },
    { home, runRoot: join(directory, 'scoped'), providerShim: shim, cwd: directory,
      nativeBinaries: { claude: '/fixture/claude', codex: '/fixture/codex' } });
  assert.equal(profile.run.run_id, runId);
  assert.equal(profile.run.schema_version, 1);
  assert.equal(profile.run.workspace, directory);
  assert.equal(profile.run.native_binaries.claude, '/fixture/claude');
  assert.equal(profile.run.native_binaries.codex, '/fixture/codex');
  assert.equal(statSync(profile.run.provider_path).mode & 0o777, 0o700);
  assert.equal(statSync(profile.filename).mode & 0o777, 0o600);
  for (const provider of ['claude', 'codex']) {
    const target = join(profile.run.provider_path, provider);
    assert.equal(readlinkSync(target), shim);
    const child = spawnSync(provider, ['--version'], { env: { PATH: profile.env.PATH }, encoding: 'utf8' });
    assert.equal(child.status, 0, child.stderr);
    assert.equal(child.stdout, `${target}\n--version\n`);
  }
  assert.deepEqual(JSON.parse(readFileSync(profile.filename, 'utf8')), profile.run);
  assert.doesNotMatch(readFileSync(profile.filename, 'utf8'), /CHAT_API_KEY|Authorization/);
});

test('status reports scoped jobs and limits global output to 200 recent files', () => {
  const stateDir = join(directory, 'native-state');
  const current = randomUUID(), other = randomUUID();
  for (const [run, count] of [[current, 2], [other, 201]]) {
    const jobs = join(stateDir, 'runs', run, 'jobs');
    mkdirSync(jobs, { recursive: true });
    for (let i = 0; i < count; i++) writeFileSync(join(jobs, `${i}.json`), JSON.stringify({ job_id: `${run}-${i}`, provider: 'codex', status: 'completed' }));
  }
  const scoped = wrapperStatus({ OMC_WRAPPER_STATE_DIR: stateDir, OMC_WRAPPER_RUN_ID: current }, home);
  assert.equal(scoped.jobs.length, 2);
  assert.ok(scoped.jobs.every(job => job.job_id.startsWith(current)));
  const global = wrapperStatus({ OMC_WRAPPER_STATE_DIR: stateDir }, home);
  assert.equal(global.jobs.length, 200);
  assert.equal(global.truncated, true);
});

test('model and status public commands finish without native jobs or launch profiles', () => {
  const childEnv = { ...process.env, HOME: home, OMC_WRAPPER_MODEL_CONFIG: join(directory, 'public-models.json'),
    XDG_STATE_HOME: join(directory, 'public-state-root'), OMC_WRAPPER_STATE_DIR: join(directory, 'empty-state'), CLAUDE_CONFIG_DIR: join(home, '.claude'), CODEX_HOME: join(home, '.codex') };
  delete childEnv.OMC_WRAPPER_RUN_FILE;
  delete childEnv.OMC_WRAPPER_RUN_ID;
  const launches = join(childEnv.XDG_STATE_HOME || join(home, '.local/state'), 'omc-provider-wrapper/launches');
  const before = existsSync(launches) ? readdirSync(launches).length : 0;
  function command(args) {
    const child = spawnSync(process.execPath, [launcher, ...args], { env: childEnv, encoding: 'utf8' });
    assert.equal(child.status, 0, child.stderr);
    return child.stdout;
  }
  assert.match(command(['models', 'set', 'codex', 'public/custom-model']), /public\/custom-model/);
  assert.equal(JSON.parse(command(['models', 'show'])).saved.codex, 'public/custom-model');
  const listed = command(['models', 'list']);
  assert.match(listed, /not an account entitlement list/);
  assert.match(listed, /claude-opus-5-5/);
  assert.match(listed, /claude-opus-4-8/);
  assert.deepEqual(JSON.parse(command(['wrapper', 'status', '--json'])).jobs, []);
  assert.equal(existsSync(launches) ? readdirSync(launches).length : 0, before);
});

test('direct wrapperRequest uses only explicitly supplied model and effort', async t => {
  let received;
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received = JSON.parse(Buffer.concat(chunks));
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ provider: 'codex', exit_code: 0, dry_run: false, text: 'fixture advice' }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const config = { endpoint: new URL(`http://127.0.0.1:${server.address().port}`), key: 'fixture-key', timeout: 5 };
  await wrapperRequest(config, 'codex', 'fixture');
  assert.equal(received.model, undefined);
  assert.equal(received.effort, undefined);
  await wrapperRequest({ ...config, model: 'exact/custom-id', effort: 'ultra' }, 'codex', 'fixture');
  assert.equal(received.model, 'exact/custom-id');
  assert.equal(received.effort, 'ultra');
});

test('installed OMC forwards explicit advisor selection and keeps prompt flags literal', async t => {
  let received;
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received = JSON.parse(Buffer.concat(chunks));
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ provider: 'codex', exit_code: 0, dry_run: false, text: 'fixture advice', request_id: 'selected-fixture' }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const credentials = join(directory, 'service.env');
  writeFileSync(credentials, 'CHAT_API_KEY=fixture-key\n');
  const childEnv = { ...process.env, HOME: home, CLAUDE_CONFIG_DIR: process.env.CLAUDE_CONFIG_DIR || join(process.env.HOME, '.claude'), OMC_WRAPPER_MODEL_CONFIG: configFile,
    OMC_WRAPPER_URL: `http://127.0.0.1:${server.address().port}`, OMC_WRAPPER_ENV_FILE: credentials,
    OMC_STATE_DIR: join(directory, 'omc-state'), CODEX_HOME: join(home, '.codex') };
  const child = spawn(process.execPath, [launcher, 'ask', 'codex', '--model', 'advisor/custom-id', '--effort', 'xhigh', '--agent-prompt', 'critic', '--prompt', 'Review fixture --model literal'],
    { cwd: directory, env: childEnv, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', data => { stdout += data; });
  child.stderr.on('data', data => { stderr += data; });
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  assert.equal(code, 0, stderr);
  assert.equal(received.model, 'advisor/custom-id');
  assert.equal(received.effort, 'xhigh');
  assert.ok(received.prompt.endsWith('Review fixture --model literal'));
  assert.ok(received.prompt.length > 500);
  const metadata = JSON.parse(readFileSync(stdout.trim().replace(/\.md$/, '.json'), 'utf8'));
  assert.equal(metadata.requested_model, 'advisor/custom-id');
  assert.equal(metadata.requested_effort, 'xhigh');
  assert.ok(metadata.run_id);
});
