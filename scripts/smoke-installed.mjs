#!/usr/bin/env node
import assert from 'node:assert/strict';
import { statSync, writeFileSync, mkdirSync, symlinkSync } from 'node:fs';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { installation } from '../omc/installation.mjs';
import { readServiceEnv } from '../omc/advisor.mjs';

const config = installation();
const env = readServiceEnv(config.service_env);
assert.equal(statSync(config.service_env).mode & 0o777, 0o600);
assert.equal(statSync(join(process.env.OMC_WRAPPER_CONFIG_DIR, 'install.json')).mode & 0o777, 0o600);
const fixtures = join(process.env.OMC_WRAPPER_CONFIG_DIR, 'offline-fixtures');
mkdirSync(fixtures, { recursive: true });
for (const provider of ['claude', 'codex']) {
  try { symlinkSync(fileURLToPath(new URL('./fixture-provider.sh', import.meta.url)), join(fixtures, provider)); } catch (error) { if (error.code !== 'EEXIST') throw error; }
  env[`CHAT_API_${provider.toUpperCase()}_BIN`] = join(fixtures, provider);
}
// Exercise the installed foreground launcher with a separate fixture config.
// Do not replace the installing user's environment file or native binaries.
const fixtureConfig = join(fixtures, 'config');
mkdirSync(fixtureConfig, { recursive: true, mode: 0o700 });
const fixtureEnv = join(fixtureConfig, 'service.env');
writeFileSync(fixtureEnv, Object.entries(env).map(([key, value]) => `${key}="${value}"`).join('\n') + '\n', { mode: 0o600 });
writeFileSync(join(fixtureConfig, 'install.json'), JSON.stringify({ ...config, service_env: fixtureEnv }), { mode: 0o600 });
const server = spawn(process.execPath, [join(config.root, 'scripts/server.mjs')], {
  env: { ...process.env, OMC_WRAPPER_CONFIG_DIR: fixtureConfig }, stdio: ['ignore', 'pipe', 'pipe'],
});
let errors = '';
server.stderr.on('data', data => { errors += data; });
try {
  let ready;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { ready = await fetch(`${config.url}/health`); if (ready.ok) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready?.ok, errors);
  const unauthorized = await fetch(`${config.url}/api/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: 'fixture' }) });
  assert.equal(unauthorized.status, 401);
  for (const provider of ['claude', 'codex']) {
    const response = await fetch(`${config.url}/api/chat`, { method: 'POST', headers: { Authorization: `Bearer ${env.CHAT_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, prompt: 'offline fixture', fallback: false }) });
    const data = await response.json();
    assert.equal(response.status, 200, JSON.stringify(data));
    assert.equal(data.provider, provider);
    assert.equal(data.text, 'OFFLINE_WRAPPER_OK');
    assert.equal(data.provider_status.outcome, 'completed');
    const advisor = spawnSync(join(process.env.OMC_WRAPPER_BIN_DIR, 'omc'), ['ask', provider, '--prompt', 'offline fixture'],
      { cwd: fixtures, env: process.env, encoding: 'utf8', timeout: 15000 });
    assert.equal(advisor.status, 0, advisor.stderr);
    assert.match(advisor.stderr, /omc-provider-status/);
  }
  const model = spawnSync(join(process.env.OMC_WRAPPER_BIN_DIR, 'omc'), ['models', 'set', 'codex', 'fixture/custom-id'], { encoding: 'utf8', env: process.env });
  assert.equal(model.status, 0, model.stderr);
  const health = await fetch(`${config.url}/api/readiness`, { headers: { Authorization: `Bearer ${env.CHAT_API_KEY}` } });
  assert.equal(health.status, 200, await health.text());
  const launch = spawnSync(join(process.env.OMC_WRAPPER_BIN_DIR, 'omc'), ['--claude-model', 'claude-fixture', '--codex-model', 'codex-fixture', 'launch', '--yolo', '-p', 'offline fixture'],
    { cwd: fixtures, env: { ...process.env, OMC_WRAPPER_CLAUDE_BIN: join(fixtures, 'claude'), OMC_WRAPPER_CODEX_BIN: join(fixtures, 'codex') },
      input: 'offline fixture', encoding: 'utf8', timeout: 15000 });
  assert.equal(launch.status, 0, launch.stderr);
  assert.match(launch.stdout, /OFFLINE_WRAPPER_OK/);
  console.log('Installed smoke: auth rejection, both HTTP + OMC advisor routes, readiness, model selection and native OMC launch passed.');
} finally {
  server.kill('SIGTERM');
  await new Promise(resolve => server.once('exit', resolve));
}
