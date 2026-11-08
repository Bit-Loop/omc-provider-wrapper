import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync, chmodSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { applyProviderConfig, hermesSteps, renderClaudeRoute, renderOverlay, renderServiceEnv } from './provider-apply.mjs';
import { providerCommand, resolveModel, splitSlashArguments } from './provider-command.mjs';
import { loadProviderConfig, permitted, providerConfigPath, routeSlug, saveProviderConfig } from './provider-config.mjs';
import { savedModels, saveModels } from './models.mjs';

const CATALOG = ['openrouter/z-ai/glm-5.3', 'openrouter/z-ai/glm-5.3-flash', 'openrouter/google/gemini-omni', 'ollama/qwen3:30b'];

function fixture(t) {
  const home = mkdtempSync(join(tmpdir(), 'omc-provider-'));
  const bin = join(home, 'bin');
  mkdirSync(bin);
  const opencode = join(bin, 'opencode');
  writeFileSync(opencode, '#!/bin/sh\nexit 0\n');
  chmodSync(opencode, 0o755);
  const env = { HOME: home, PATH: `${bin}:/usr/bin:/bin`, XDG_CONFIG_HOME: join(home, '.config'), OMC_WRAPPER_OPENCODE_BIN: opencode };
  const lines = [];
  const options = { catalogLoader: async () => CATALOG, reload: false };
  const run = (...args) => providerCommand(args, { env, home, print: line => lines.push(line), options });
  return { home, env, lines, options, run, overlay: () => JSON.parse(readFileSync(join(home, '.config/opencode/opencode.jsonc'), 'utf8').replace(/^\/\/.*\n/, '')) };
}

test('permitted patterns treat only * as special and ignore variants', () => {
  assert.equal(permitted('a/b', null), true);
  assert.equal(permitted('a/b', ['a/b']), true);
  assert.equal(permitted('a/bc', ['a/b']), false);
  assert.equal(permitted('z/glm.5', ['z/glm-5']), false, 'a dot is literal');
  assert.equal(permitted('openrouter/z-ai/glm-5.3', ['openrouter/z-ai/*']), true);
  assert.equal(permitted('a/b#high', ['a/b']), true);
});

test('provider settings work with only OpenCode on PATH and without any native CLI', async t => {
  const { home, env, run, overlay } = fixture(t);
  delete env.OMC_WRAPPER_OPENCODE_BIN;
  env.PATH = join(home, 'bin');
  await run('model', 'openrouter/z-ai/glm-5.3');
  assert.equal(overlay().model, 'openrouter/z-ai/glm-5.3');
  env.PATH = '';
  await applyProviderConfig({ env, home, reload: false });
  assert.equal(overlay().model, 'openrouter/z-ai/glm-5.3');
});

test('an allow-list disables every other catalog model, because whitelist is not enforced', () => {
  const config = { ...loadProviderConfig({}, '/nonexistent'), opencode: { allowed_models: ['openrouter/z-ai/*'], apis: null, default_model: 'openrouter/z-ai/glm-5.3' } };
  const overlay = renderOverlay(config, { catalog: CATALOG });
  assert.equal(overlay.model, 'openrouter/z-ai/glm-5.3');
  assert.deepEqual(overlay.providers.openrouter.models, { 'google/gemini-omni': { disabled: true } });
  assert.deepEqual(overlay.providers.ollama.models, { 'qwen3:30b': { disabled: true } });
  assert.equal(JSON.stringify(overlay).includes('whitelist'), false);
});

test('no settings render no opinions, so a fresh overlay changes nothing', () => {
  const overlay = renderOverlay(loadProviderConfig({}, '/nonexistent'));
  assert.deepEqual(Object.keys(overlay), ['$schema']);
});

test('APIs render with env references and never literal keys', () => {
  const config = { ...loadProviderConfig({}, '/nonexistent'), apis: { lab: { base_url: 'http://127.0.0.1:1/v1', key_env: 'LAB_KEY' } } };
  const provider = renderOverlay(config, { apiModels: { lab: ['a'] } }).providers.lab;
  assert.equal(provider.settings.apiKey, '{env:LAB_KEY}');
  assert.equal(provider.settings.baseURL, 'http://127.0.0.1:1/v1');
  assert.equal(provider.models.a.capabilities.tools, true);
});

test('routes pin a subagent to their model in OpenCode', () => {
  const config = { ...loadProviderConfig({}, '/nonexistent'), routes: [{ task: 'Code Review!', model: 'openrouter/z-ai/glm-5.3#max' }] };
  const agent = renderOverlay(config).agents['route-code-review'];
  assert.deepEqual(agent.model, { providerID: 'openrouter', model: 'z-ai/glm-5.3', variant: 'max' });
  assert.equal(agent.mode, 'subagent');
  assert.match(agent.description, /Code Review!/);
});

test('Claude routes use native models, and other models delegate through the wrapper', () => {
  const native = renderClaudeRoute({ task: 'docs', model: 'sonnet' });
  assert.match(native, /^model: sonnet$/m);
  assert.doesNotMatch(native, /omc ask/);
  const delegated = renderClaudeRoute({ task: 'docs', model: 'openrouter/z-ai/glm-5.3' }, '/bin/omc');
  assert.match(delegated, /^model: haiku$/m);
  assert.match(delegated, /'\/bin\/omc' ask opencode --model 'openrouter\/z-ai\/glm-5\.3'/);
  assert.match(delegated, /^tools: Bash, Read$/m);
});

test('generated Claude delegation executes an OMC path containing an apostrophe literally', t => {
  const directory = mkdtempSync(join(tmpdir(), 'omc-route-shell-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const bin = join(directory, "user's bin");
  mkdirSync(bin);
  const omc = join(bin, 'omc');
  writeFileSync(omc, '#!/bin/sh\nprintf "%s\\n" "$@"\n', { mode: 0o755 });
  const route = renderClaudeRoute({ task: 'docs', model: 'fixture/model' }, omc);
  const command = route.match(/```bash\n([\s\S]*?)```/)[1];
  const result = spawnSync('/bin/sh', ['-c', command], { encoding: 'utf8', timeout: 3000 });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.stdout.trim().split('\n'), ['ask', 'opencode', '--model', 'fixture/model', '--prompt', 'TASK']);
});

test('Hermes steps use its named-provider form and its own key mechanism', () => {
  const config = { ...loadProviderConfig({}, '/nonexistent'),
    apis: { lab: { base_url: 'http://127.0.0.1:1/v1', key_env: 'LAB_KEY' } }, hermes: { api: 'lab', model: 'm' } };
  assert.deepEqual(hermesSteps(config), [['set', 'model.default', 'm'], ['set', 'providers.lab.base_url', 'http://127.0.0.1:1/v1'],
    ['set', 'model.provider', 'lab'], ['unset', 'model.base_url'], ['set', 'providers.lab.key_env', 'LAB_KEY']]);
});

test('service.env edits keep the credential and every other line', () => {
  const content = 'CHAT_API_KEY="secret"\nCHAT_API_DEFAULT_PROVIDER="codex"\nPATH="/bin"\n';
  assert.equal(renderServiceEnv(content, { default_provider: 'opencode', fallback_providers: ['codex', 'claude'] }),
    'CHAT_API_KEY="secret"\nCHAT_API_DEFAULT_PROVIDER="opencode"\nPATH="/bin"\nCHAT_API_FALLBACK_PROVIDERS="codex,claude"\n');
  assert.equal(renderServiceEnv(content, {}), content);
});

test('model names resolve exactly, then by a unique fragment', () => {
  assert.equal(resolveModel('openrouter/z-ai/glm-5.3', CATALOG), 'openrouter/z-ai/glm-5.3');
  assert.equal(resolveModel('openrouter/z-ai/glm-5.3#max', CATALOG), 'openrouter/z-ai/glm-5.3#max');
  assert.equal(resolveModel('GEMINI', CATALOG), 'openrouter/google/gemini-omni');
  assert.throws(() => resolveModel('glm', CATALOG), /matches 2 models/);
  assert.throws(() => resolveModel('nope', CATALOG), /No permitted model/);
});

test('slash arguments respect quotes', () => {
  assert.deepEqual(splitSlashArguments(`route add "code review" a/b`), ['route', 'add', 'code review', 'a/b']);
  assert.deepEqual(splitSlashArguments(`route add 'it is' a/b`), ['route', 'add', 'it is', 'a/b']);
});

test('settings are private and reject secrets where a name belongs', async t => {
  const { run, env, home } = fixture(t);
  await run('api', 'add', 'lab', 'http://127.0.0.1:1/v1', '--key-env', 'LAB_KEY', '--models', 'a,b');
  assert.equal(statSync(providerConfigPath(env, home)).mode & 0o777, 0o600);
  await assert.rejects(run('api', 'add', 'bad', 'http://127.0.0.1:1/v1', '--key-env', 'sk-live-abc'), /variable name/);
  await assert.rejects(run('api', 'add', 'bad', 'http://user:pw@127.0.0.1/v1'), /without embedded credentials/);
  await assert.rejects(run('api', 'add', 'bad', 'file:///etc/passwd'), /http\(s\)/);
});

test('apply writes the overlay and leaves the user file alone', async t => {
  const { run, home, overlay } = fixture(t);
  const userFile = join(home, '.config/opencode/opencode.json');
  mkdirSync(join(home, '.config/opencode'), { recursive: true });
  const original = JSON.stringify({ model: 'llama-local/x', provider: { 'llama-local': { models: { x: {} } } } });
  writeFileSync(userFile, original);
  await run('allow', 'set', 'openrouter/z-ai/*');
  await run('model', 'glm-5.3-flash');
  assert.equal(readFileSync(userFile, 'utf8'), original);
  assert.equal(overlay().model, 'openrouter/z-ai/glm-5.3-flash');
  assert.deepEqual(overlay().providers['llama-local'].models.x, { disabled: true }, 'user models obey the allow-list');
  assert.ok(existsSync(join(home, '.config/opencode/commands/model.md')));
  assert.ok(existsSync(join(home, '.config/opencode/commands/provider.md')));
  assert.ok(existsSync(join(home, '.claude/skills/provider/SKILL.md')));
});

test('default and routes must stay inside the permitted models', async t => {
  const { run } = fixture(t);
  await run('allow', 'set', 'openrouter/z-ai/*');
  await assert.rejects(run('model', 'gemini'), /No permitted model/);
  await assert.rejects(run('route', 'add', 'x', 'openrouter/google/gemini-omni'), /not a permitted model/);
  await run('model', 'glm-5.3-flash');
  await assert.rejects(run('allow', 'set', 'ollama/*'), /exclude the default model/);
  await assert.rejects(run('allow', 'remove', 'openrouter/z-ai/*'), /permit no models/);
});

test('routes are durable, replaceable and clean up their generated files', async t => {
  const { run, home, overlay, env } = fixture(t);
  await run('route', 'add', 'code review', 'openrouter/z-ai/glm-5.3');
  await run('route', 'add', 'Code  Review', 'sonnet');
  assert.deepEqual(loadProviderConfig(env, home).routes, [{ task: 'Code  Review', model: 'sonnet' }], 'same slug replaces');
  assert.equal(overlay().agents, undefined, 'a Claude-only route has no OpenCode agent');
  const agent = join(home, '.claude/agents/omc-route-code-review.md');
  assert.match(readFileSync(agent, 'utf8'), /^model: sonnet$/m);
  await run('route', 'remove', 'code review');
  assert.equal(existsSync(agent), false);
});

test('files the user owns are never replaced or removed', async t => {
  const { run, home, env, options, lines } = fixture(t);
  const skill = join(home, '.claude/skills/provider/SKILL.md');
  mkdirSync(join(home, '.claude/skills/provider'), { recursive: true });
  writeFileSync(skill, 'mine');
  await run('apply');
  assert.equal(readFileSync(skill, 'utf8'), 'mine');
  assert.ok(lines.some(line => line.includes('Left your file in place')));
  const overlayFile = join(home, '.config/opencode/opencode.jsonc');
  writeFileSync(overlayFile, '{ "model": "mine/x" }');
  await assert.rejects(applyProviderConfig({ env, home, ...options }), /your own file/);
  assert.equal(readFileSync(overlayFile, 'utf8'), '{ "model": "mine/x" }');
});

test('a setting that cannot be applied is not kept, and later commands still work', async t => {
  const { run, env, home, lines } = fixture(t);
  const hermes = join(home, 'bin/hermes');
  writeFileSync(hermes, `#!/bin/sh
case "$*" in *no-such-profile*) echo "Error: Profile does not exist" >&2; exit 1;; esac
echo "$*" >> '${home}/hermes.log'
case "$*" in *unset*) echo "Config key not set: model.base_url" >&2; exit 1;; esac
exit 0
`);
  chmodSync(hermes, 0o755);
  env.OMC_WRAPPER_HERMES_BIN = hermes;
  await run('hermes', '--api', 'ollama', '--model', 'good');
  const good = loadProviderConfig(env, home).hermes;
  assert.deepEqual(good, { api: 'ollama', model: 'good' }, 'an unset of a missing key is not a failure');
  await assert.rejects(run('hermes', '--profile', 'no-such-profile', '--model', 'bad'), /does not exist/);
  assert.deepEqual(loadProviderConfig(env, home).hermes, good);
  await run('route', 'add', 'x', 'a/b');
  assert.equal(loadProviderConfig(env, home).routes.length, 1);
  await assert.rejects(run('model', 'nope'), /No permitted model/);
  assert.ok(lines.length);
});

test('hermes only runs when its settings change', async t => {
  const { run, env, home } = fixture(t);
  const hermes = join(home, 'bin/hermes');
  writeFileSync(hermes, `#!/bin/sh\necho "$*" >> '${home}/hermes.log'\n`);
  chmodSync(hermes, 0o755);
  env.OMC_WRAPPER_HERMES_BIN = hermes;
  await run('hermes', '--profile', 'p', '--model', 'm');
  await run('route', 'add', 'x', 'a/b');
  await run('apply');
  assert.deepEqual(readFileSync(join(home, 'hermes.log'), 'utf8').trim().split('\n'), ['-p p config set --force model.default m']);
});

test('a dry run writes nothing', async t => {
  const { run, home, lines } = fixture(t);
  await run('apply', '--dry-run');
  assert.ok(lines.join('\n').includes('opencode.jsonc'));
  assert.equal(existsSync(join(home, '.config/opencode/opencode.jsonc')), false);
});

test('an unreachable API keeps its last known models', async t => {
  const { run, overlay } = fixture(t);
  await run('api', 'add', 'lab', 'http://127.0.0.1:9/v1', '--models', 'a');
  const config = overlay();
  assert.deepEqual(Object.keys(config.providers.lab.models), ['a']);
});

test('settings outlive a reinstall: apply rebuilds everything from providers.json', async t => {
  const { run, home, env, options, overlay } = fixture(t);
  await run('allow', 'set', 'openrouter/z-ai/*');
  await run('model', 'glm-5.3-flash');
  const before = readFileSync(join(home, '.config/opencode/opencode.jsonc'), 'utf8');
  for (const name of ['opencode.jsonc', 'commands/model.md']) writeFileSync(join(home, '.config/opencode', name), `// omc-provider-wrapper generated\n{}`);
  await applyProviderConfig({ env, home, ...options });
  assert.equal(readFileSync(join(home, '.config/opencode/opencode.jsonc'), 'utf8'), before);
  assert.equal(overlay().model, 'openrouter/z-ai/glm-5.3-flash');
});

test('saving another provider never pins OpenCode to its inherited model', t => {
  const { env, home } = fixture(t);
  mkdirSync(join(home, '.config/opencode'), { recursive: true });
  writeFileSync(join(home, '.config/opencode/opencode.json'), JSON.stringify({ model: 'llama-local/x' }));
  assert.equal(savedModels(env, home).opencode, 'llama-local/x');
  saveModels({ ...savedModels(env, home), claude: 'sonnet' }, env, home);
  assert.equal(loadProviderConfig(env, home).opencode.default_model, null);
  writeFileSync(join(home, '.config/opencode/opencode.json'), JSON.stringify({ model: { providerID: 'p', model: 'm' } }));
  assert.equal(savedModels(env, home).opencode, 'p/m');
});

test('a newer settings schema refuses to load instead of dropping data', t => {
  const { env, home } = fixture(t);
  saveProviderConfig({ future_key: true, routes: [] }, env, home);
  assert.equal(loadProviderConfig(env, home).future_key, true, 'unknown keys survive');
  writeFileSync(providerConfigPath(env, home), JSON.stringify({ schema_version: 99 }));
  assert.throws(() => loadProviderConfig(env, home), /Update the wrapper/);
});

test('model references cannot carry shell syntax into generated agents', async t => {
  const { run } = fixture(t);
  for (const model of [`a/b'; rm -rf ~; '`, 'a/$(id)', 'a/`id`', 'a/b c', 'a/b"c']) {
    await assert.rejects(run('route', 'add', 'x', model), /Route models/);
  }
  await assert.rejects(run('allow', 'set', `a/b'`), /Permitted models/);
  await run('route', 'add', 'x', 'ollama/obliteratus/feyospace-s1-obliterated:imatrix-q4_k_m');
  await run('allow', 'set', 'openrouter/z-ai/*', 'ollama/*');
});

test('update retains recorded install paths and forwards installer flags', async t => {
  const { env, home, run } = fixture(t);
  const source = join(home, 'source');
  const cfg = join(home, '.config/omc-provider-wrapper');
  mkdirSync(source);
  mkdirSync(cfg, { recursive: true });
  const recorded = { source_dir: source, root: join(home, 'custom-install'), bin_dir: join(home, 'custom-bin'),
    claude_config_dir: join(home, 'custom-claude'), opencode_config_dir: join(home, 'custom-opencode'),
    xdg_config_home: join(home, 'custom-xdg'), xdg_state_home: join(home, 'custom-state') };
  writeFileSync(join(cfg, 'install.json'), JSON.stringify(recorded));
  const log = join(source, 'installer-arguments');
  writeFileSync(join(source, 'install.sh'), `#!/bin/sh\nprintf '%s\\n' "$OMC_WRAPPER_INSTALL_DIR" "$OMC_WRAPPER_BIN_DIR" "$OMC_WRAPPER_CONFIG_DIR" "$CLAUDE_CONFIG_DIR" "$OPENCODE_CONFIG_DIR" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$@" > '${log}'\n`, { mode: 0o755 });
  // The existing process explicitly selects this XDG root; recorded paths fill
  // only values absent from its environment.
  await run('update', '--no-service', '--no-providers');
  assert.deepEqual(readFileSync(log, 'utf8').trim().split('\n'), [recorded.root, recorded.bin_dir, cfg,
    recorded.claude_config_dir, recorded.opencode_config_dir, env.XDG_CONFIG_HOME, recorded.xdg_state_home,
    '--no-service', '--no-providers']);
  await assert.rejects(run('update', '--source'), /needs a directory/);
});

test('route slugs are bounded and never empty', () => {
  assert.equal(routeSlug('!!!'), 'task');
  assert.ok(routeSlug('x'.repeat(200)).length <= 48);
});
