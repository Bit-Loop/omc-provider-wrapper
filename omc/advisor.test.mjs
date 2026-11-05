import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readServiceEnv, wrapperConfig, wrapperRequest } from './advisor.mjs';
import { pluginRoot } from './runtime.mjs';

const directory = mkdtempSync(join(tmpdir(), 'omc-wrapper-test-'));
const envFile = join(directory, 'service.env');
const codexFixture = join(directory, 'codex');
mkdirSync(join(codexFixture, 'prompts'), { recursive: true });
writeFileSync(join(codexFixture, 'prompts/critic.md'), readFileSync(join(pluginRoot(), 'agents/critic.md')));
writeFileSync(envFile, '# test\nCHAT_API_KEY="fixture-private-key"\nCHAT_API_TIMEOUT_SECONDS=90\nCHAT_API_MAX_TIMEOUT_SECONDS=120\n', { mode: 0o600 });
test.after(() => rmSync(directory, { recursive: true, force: true }));

async function serverFor(t, handler) {
  const server = createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  return { endpoint: new URL(`http://127.0.0.1:${server.address().port}`), key: 'fixture-private-key', timeout: 5 };
}

test('reads credentials without evaluating shell syntax', () => {
  assert.equal(readServiceEnv(envFile).CHAT_API_KEY, 'fixture-private-key');
  const file = join(directory, 'literal.env');
  writeFileSync(file, "CHAT_API_KEY='$(touch should-never-exist)'\n");
  assert.equal(readServiceEnv(file).CHAT_API_KEY, '$(touch should-never-exist)');
});

test('caps timeout and restricts authenticated requests to numeric loopback origins', () => {
  assert.equal(wrapperConfig({ OMC_WRAPPER_ENV_FILE: envFile, OMC_WRAPPER_TIMEOUT_SECONDS: '600' }).timeout, 120);
  for (const url of ['https://127.0.0.1', 'http://example.com', 'http://localhost', 'http://127.0.0.1@evil.example', 'http://127.0.0.1/base']) {
    assert.throws(() => wrapperConfig({ OMC_WRAPPER_ENV_FILE: envFile, OMC_WRAPPER_URL: url }), /numeric loopback/);
  }
});

for (const provider of ['claude', 'codex']) {
  test(`${provider} request preserves wrapper boundaries and multiline context`, async t => {
    let received;
    const config = await serverFor(t, async (req, res) => {
      assert.equal(req.url, '/api/chat');
      assert.equal(req.headers.authorization, 'Bearer fixture-private-key');
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      received = JSON.parse(Buffer.concat(chunks));
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ provider, exit_code: 0, dry_run: false, text: 'OK', request_id: 'fixture-id' }));
    });
    const prompt = '---\nrole: critic\n---\nReview `literal` and $(literal).';
    const result = await wrapperRequest(config, provider, prompt);
    assert.equal(result.text, 'OK');
    assert.equal(received.provider, provider);
    assert.equal(received.prompt, prompt);
    assert.equal(received.allow_tools, false);
    assert.equal(received.safe_guard, true);
    assert.equal(received.fallback, false);
    assert.equal(received.persist, false);
    assert.equal(received.codex_sandbox, 'read-only');
    assert.equal(received.cwd, undefined);
  });
}

test('HTTP errors fail and redact credentials', async t => {
  const config = await serverFor(t, (req, res) => {
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'failure fixture-private-key' } }));
  });
  await assert.rejects(wrapperRequest(config, 'claude', 'test'), error => {
    assert.match(error.message, /HTTP 502/);
    assert.doesNotMatch(error.message, /fixture-private-key/);
    return true;
  });
});

test('redirects fail without sending credentials to another endpoint', async t => {
  const config = await serverFor(t, (req, res) => { res.writeHead(302, { Location: '/other' }); res.end(); });
  await assert.rejects(wrapperRequest(config, 'codex', 'test'));
});

test('wrong provider, failed exit, dry run, fallback and empty output cannot report success', async t => {
  const responses = [
    { provider: 'claude' }, { exit_code: 1 }, { dry_run: true },
    { fallback: { used: true } }, { fallback: { final: true } }, { text: '  ' },
  ];
  const config = await serverFor(t, (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ provider: 'codex', exit_code: 0, dry_run: false, text: 'OK', ...responses.shift() }));
  });
  for (let i = 0; i < 6; i++) await assert.rejects(wrapperRequest(config, 'codex', 'test'), /did not return a successful/);
});

test('successful output redacts a leaked service credential', async t => {
  const config = await serverFor(t, (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ provider: 'claude', exit_code: 0, dry_run: false, text: 'output fixture-private-key' }));
  });
  assert.equal((await wrapperRequest(config, 'claude', 'test')).text, 'output [REDACTED]');
});

async function runOmc(endpoint, args) {
  const child = spawn(process.execPath, [fileURLToPath(new URL('./omc.mjs', import.meta.url)), ...args], {
    cwd: directory,
    env: { ...process.env, OMC_ASK_ADVISOR_SCRIPT: fileURLToPath(new URL('./advisor.mjs', import.meta.url)),
      OMC_WRAPPER_URL: String(endpoint), OMC_WRAPPER_ENV_FILE: envFile,
      OMC_STATE_DIR: join(directory, 'state'), CODEX_HOME: codexFixture, OMC_WRAPPER_TIMEOUT_SECONDS: '5' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '', stderr = '';
  child.stdout.on('data', data => { stdout += data; });
  child.stderr.on('data', data => { stderr += data; });
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  return { code, stdout, stderr };
}

test('installed OMC forwards role prompts and saves provenance in its artifact', async t => {
  let prompt;
  const config = await serverFor(t, async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    prompt = JSON.parse(Buffer.concat(chunks)).prompt;
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ provider: 'codex', exit_code: 0, dry_run: false, text: 'FIXTURE_ADVICE', request_id: 'integration-fixture-id', cwd: '/isolated-wrapper-workspace' }));
  });
  const child = await runOmc(config.endpoint, ['ask', 'codex', '--agent-prompt', 'critic', 'Review fixture plan']);
  assert.equal(child.code, 0, child.stderr);
  assert.ok(prompt.length > 500, 'OMC role body must be included');
  assert.ok(prompt.endsWith('Review fixture plan'));
  const artifact = readFileSync(child.stdout.trim(), 'utf8');
  assert.match(artifact, /integration-fixture-id/);
  assert.match(artifact, /FIXTURE_ADVICE/);
  assert.match(artifact, /## Original task\n\nReview fixture plan/);
  assert.match(artifact, /isolated-wrapper-workspace/);
});

test('installed OMC propagates wrapper failure and writes a failure artifact', async t => {
  const config = await serverFor(t, (req, res) => {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'fixture-unavailable fixture-private-key' } }));
  });
  const child = await runOmc(config.endpoint, ['ask', 'claude', 'Fail fixture request']);
  assert.equal(child.code, 1);
  const artifact = readFileSync(child.stdout.trim(), 'utf8');
  assert.match(artifact, /Exit code: 1/);
  assert.match(artifact, /Wrapper request failed: Wrapper HTTP 503/);
  assert.match(child.stderr, /fixture-unavailable/);
  assert.doesNotMatch(artifact + child.stderr, /fixture-private-key/);
});

test('cyber block metadata reaches the orchestrator on failure', async t => {
  const status = { schema_version: 1, outcome: 'blocked', requested_model: 'gpt-test', actual_model: null,
    actual_model_source: 'unknown', model_switches: [], safety_flags: [],
    block: { detected: true, category: 'cybersecurity', source: 'provider_event' } };
  const config = await serverFor(t, (req, res) => {
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { code: 'provider_cyber_block', message: 'Provider blocked the request.', retryable: false,
      details: { request_id: 'cyber-fixture-id', provider: 'codex', provider_status: status } } }));
  });
  const child = await runOmc(config.endpoint, ['ask', 'codex', 'Fixture block']);
  assert.equal(child.code, 1);
  assert.match(child.stderr, /provider_cyber_block/);
  assert.match(child.stderr, /cybersecurity/);
  const artifact = readFileSync(child.stdout.trim(), 'utf8');
  assert.match(artifact, /cyber-fixture-id/);
  assert.match(artifact, /Provider status/);
  const metadata = JSON.parse(readFileSync(child.stdout.trim().replace(/\.md$/, '.json'), 'utf8'));
  assert.deepEqual(metadata.provider_status, status);
  assert.equal(metadata.error.code, 'provider_cyber_block');
});

test('observed Claude fallback is visible immediately and in the artifact', async t => {
  const status = { schema_version: 1, outcome: 'completed', requested_model: 'opus', actual_model: 'claude-opus-4-8',
    actual_model_source: 'assistant_event', block: { detected: false },
    model_switches: [{ from: 'claude-opus-5-5', to: 'claude-opus-4-8', reason: 'cybersecurity', source: 'provider_event' }] };
  const config = await serverFor(t, (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ provider: 'claude', exit_code: 0, dry_run: false, text: 'Advice', provider_status: status }));
  });
  const child = await runOmc(config.endpoint, ['ask', 'claude', 'Fixture fallback']);
  assert.equal(child.code, 0, child.stderr);
  assert.match(child.stderr, /claude-opus-4-8/);
  assert.match(child.stderr, /cybersecurity/);
  const artifact = readFileSync(child.stdout.trim(), 'utf8');
  assert.match(artifact, /claude-opus-5-5/);
  assert.match(artifact, /claude-opus-4-8/);
});

test('HTTP 200 cannot hide a blocked provider status', async t => {
  const config = await serverFor(t, (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ provider: 'codex', exit_code: 0, dry_run: false, text: '',
      provider_status: { outcome: 'blocked', reason_code: 'provider_cyber_block', block: { detected: true, category: 'cybersecurity' } } }));
  });
  const child = await runOmc(config.endpoint, ['ask', 'codex', 'Inconsistent outcome fixture']);
  assert.equal(child.code, 1);
  assert.match(child.stderr, /provider_cyber_block/);
});
