import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync, readFileSync, realpathSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { openCodeCatalog, renderFiles, renderOverlay } from './provider-apply.mjs';
import { loadProviderConfig } from './provider-config.mjs';

// Pins the OpenCode behaviors the wrapper depends on, against the real binary and
// a local mock, so an OpenCode upgrade that changes one fails here. The legacy
// `whitelist` key was silently dropped by OpenCode 2, which is the failure this catches.
function realBinary() {
  if (process.env.OMC_WRAPPER_OPENCODE_BIN) return process.env.OMC_WRAPPER_OPENCODE_BIN;
  // The last match skips launcher scripts earlier on PATH.
  return (process.env.PATH || '').split(delimiter).map(directory => join(directory, 'opencode')).filter(existsSync).at(-1);
}
const binary = realBinary();
const skip = binary ? false : 'OpenCode is not installed';

function mockProvider() {
  const requests = [];
  const server = createServer((request, response) => {
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      if (request.method === 'GET') {
        response.setHeader('content-type', 'application/json');
        return response.end(JSON.stringify({ data: [{ id: 'alpha' }, { id: 'beta' }] }));
      }
      const parsed = JSON.parse(body);
      requests.push({ model: parsed.model, tools: (parsed.tools ?? []).map(tool => tool.function?.name), authorization: request.headers.authorization, body });
      response.setHeader('content-type', 'text/event-stream');
      const chunk = (delta, finish = null) => `data: ${JSON.stringify({ id: 'x', object: 'chat.completion.chunk', model: parsed.model, choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
      response.end(chunk({ role: 'assistant', content: 'PONG' }) + chunk({}, 'stop') + 'data: [DONE]\n\n');
    });
  });
  return new Promise(done => server.listen(0, '127.0.0.1', () => done({ requests, server, url: `http://127.0.0.1:${server.address().port}/v1` })));
}

function lab(t, mock) {
  const home = mkdtempSync(join(tmpdir(), 'omc-opencode-contract-'));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const env = { PATH: process.env.PATH, HOME: home, XDG_CONFIG_HOME: join(home, '.config'), XDG_DATA_HOME: join(home, '.local/share'),
    XDG_STATE_HOME: join(home, '.local/state'), XDG_CACHE_HOME: join(home, '.cache'), OPENCODE_DISABLE_AUTOUPDATE: '1',
    OPENCODE_NO_AUTOLLAMA: '1', OMC_WRAPPER_OPENCODE_BIN: binary, LAB_KEY: 'lab-key-value' };
  const directory = join(home, '.config/opencode');
  mkdirSync(directory, { recursive: true });
  const work = join(home, 'work');
  mkdirSync(work);
  // The user's file keeps the legacy shape real installs still have.
  writeFileSync(join(directory, 'opencode.json'), JSON.stringify({ model: 'mock/alpha', provider: { mock: {
    npm: '@ai-sdk/openai-compatible', name: 'Mock', options: { baseURL: mock.url, apiKey: 'user-key' },
    models: { alpha: { name: 'Alpha', tool_call: true, limit: { context: 100000, output: 4096 } },
      beta: { name: 'Beta', tool_call: true, limit: { context: 100000, output: 4096 } } } } } }));
  const run = (args, options = {}) => {
    const child = spawn(binary, ['run', '--standalone', '--format', 'json', ...args], { cwd: work, stdio: ['pipe', 'pipe', 'pipe'], env: { ...env, ...options.env } });
    let stdout = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.resume();
    child.stdin.end(options.input ?? '');
    return new Promise(done => child.on('close', code => {
      const events = stdout.split('\n').filter(Boolean).flatMap(line => { try { return [JSON.parse(line)]; } catch { return []; } });
      done({ code, events, text: events.filter(event => event.type === 'text').map(event => event.part.text).join(''), error: events.find(event => event.type === 'error')?.error });
    }));
  };
  return { home, env, directory, run };
}

function install(directory, config, env, home, models = {}) {
  const base = { ...loadProviderConfig(env, home), ...config };
  const overlay = renderOverlay({ ...base, opencode: { ...base.opencode, ...config.opencode } }, models);
  writeFileSync(join(directory, 'opencode.jsonc'), `// generated\n${JSON.stringify(overlay)}\n`);
  return overlay;
}

test('the overlay wins over opencode.json, and disabled models cannot run', { skip, timeout: 120000 }, async t => {
  const mock = await mockProvider();
  t.after(() => mock.server.close());
  const { directory, run, env, home } = lab(t, mock);
  install(directory, { opencode: { default_model: 'mock/beta', allowed_models: ['mock/beta'] } }, env, home, { catalog: ['mock/alpha', 'mock/beta'] });
  const result = await run([], { input: 'hello' });
  assert.equal(result.code, 0);
  assert.equal(result.text, 'PONG');
  assert.ok(mock.requests.some(request => request.model === 'beta' && request.tools.length), 'the overlay default ran with tools');
  assert.ok(mock.requests.every(request => request.model !== 'alpha'));
  const blocked = await run(['-m', 'mock/alpha'], { input: 'hello' });
  assert.notEqual(blocked.code, 0);
  assert.equal(blocked.error.type, 'provider.no-route');
  assert.ok(mock.requests.every(request => request.model !== 'alpha'), 'nothing reached the provider');
});

test('enabled_providers shuts off a whole provider', { skip, timeout: 120000 }, async t => {
  const mock = await mockProvider();
  t.after(() => mock.server.close());
  const { directory, run, env, home } = lab(t, mock);
  install(directory, { opencode: { apis: ['other'] } }, env, home);
  const blocked = await run(['-m', 'mock/alpha'], { input: 'hello' });
  assert.equal(blocked.error.type, 'provider.no-route');
});

test('API entries resolve {env:} keys, and routes reach the model as subagents', { skip, timeout: 120000 }, async t => {
  const mock = await mockProvider();
  t.after(() => mock.server.close());
  const { directory, run, env, home } = lab(t, mock);
  install(directory, { apis: { lab: { base_url: mock.url, key_env: 'LAB_KEY' } }, routes: [{ task: 'code review', model: 'lab/alpha' }] },
    env, home, { apiModels: { lab: ['alpha'] } });
  const result = await run(['-m', 'lab/alpha'], { input: 'hello' });
  assert.equal(result.text, 'PONG');
  assert.ok(mock.requests.every(request => request.authorization === 'Bearer lab-key-value'));
  assert.ok(mock.requests.some(request => request.body.includes('route-code-review') && request.body.includes('code review')));
});

test('the advisor agent exposes no tools and prompts arrive on stdin', { skip, timeout: 120000 }, async t => {
  const mock = await mockProvider();
  t.after(() => mock.server.close());
  const { run } = lab(t, mock);
  const content = JSON.stringify({ agents: { 'omc-advisor': { mode: 'primary', description: 'x', permissions: [{ action: '*', resource: '*', effect: 'deny' }] } } });
  const result = await run(['--agent', 'omc-advisor'], { input: 'from stdin', env: { OPENCODE_CONFIG_CONTENT: content } });
  assert.equal(result.text, 'PONG');
  assert.ok(mock.requests.length);
  assert.ok(mock.requests.every(request => request.tools.length === 0));
  assert.ok(mock.requests.some(request => request.body.includes('from stdin')));
});

test('generated commands run their shell step and keep hostile arguments literal', { skip, timeout: 120000 }, async t => {
  const mock = await mockProvider();
  t.after(() => mock.server.close());
  const { directory, env, home } = lab(t, mock);
  const bin = join(home, 'bin');
  mkdirSync(bin);
  const captured = join(home, 'captured');
  const fake = join(bin, 'omc');
  writeFileSync(fake, `#!/bin/sh\ncat > '${captured}'\necho SHELL-STEP-RAN\n`, { mode: 0o755 });
  mkdirSync(join(home, '.config/omc-provider-wrapper'), { recursive: true, mode: 0o700 });
  writeFileSync(join(home, '.config/omc-provider-wrapper/install.json'), JSON.stringify({ bin_dir: bin }));
  const files = renderFiles(loadProviderConfig(env, home), {}, env, home);
  mkdirSync(join(directory, 'commands'));
  for (const name of ['model', 'provider']) writeFileSync(join(directory, 'commands', `${name}.md`), files[join(directory, 'commands', `${name}.md`)]);
  const password = 'contract';
  const port = 18000 + (process.pid % 2000);
  const server = spawn(binary, ['serve', '--port', String(port), '--hostname', '127.0.0.1'], { cwd: home, stdio: 'ignore', env: { ...env, OPENCODE_SERVER_PASSWORD: password } });
  t.after(() => server.kill('SIGTERM'));
  const headers = { Authorization: `Basic ${Buffer.from(`opencode:${password}`).toString('base64')}`, 'content-type': 'application/json' };
  const url = path => `http://127.0.0.1:${port}${path}${path.includes('?') ? '&' : '?'}directory=${encodeURIComponent(home)}`;
  let names = [];
  for (let attempt = 0; attempt < 60 && !names.includes('model'); attempt++) {
    await new Promise(done => setTimeout(done, 250));
    names = await fetch(url('/api/command'), { headers }).then(response => response.json()).then(body => body.data.map(item => item.name), () => []);
  }
  assert.ok(names.includes('model') && names.includes('provider'), `commands were not discovered: ${names}`);
  const session = await fetch(url('/api/session'), { method: 'POST', headers, body: '{}' }).then(response => response.json());
  const hostile = `glm'; touch ${home}/pwned; echo '$(touch ${home}/pwned)`;
  await fetch(url(`/api/session/${(session.data ?? session).id}/command`), { method: 'POST', headers, body: JSON.stringify({ name: 'model', text: hostile }) });
  for (let attempt = 0; attempt < 40 && !mock.requests.length; attempt++) await new Promise(done => setTimeout(done, 250));
  assert.equal(readFileSync(captured, 'utf8').trim(), hostile);
  assert.throws(() => readFileSync(join(home, 'pwned')));
  assert.ok(mock.requests.some(request => request.body.includes('SHELL-STEP-RAN')));
});

test('the catalog sees models the installed overlay would hide', { skip, timeout: 60000 }, async t => {
  const mock = await mockProvider();
  t.after(() => mock.server.close());
  const { directory, env, home } = lab(t, mock);
  install(directory, { opencode: { apis: ['nothing'] } }, env, home);
  const catalog = await openCodeCatalog(realpathSync(binary), env);
  assert.ok(catalog.length > 0, 'the catalog bypasses the overlay');
});
