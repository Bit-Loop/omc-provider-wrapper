#!/usr/bin/env node
import { readFileSync, realpathSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { pluginRoot } from './runtime.mjs';
import { requestSelection } from './models.mjs';
import { configRoot, installation } from './installation.mjs';

export function readServiceEnv(filename) {
  const values = {};
  for (const line of readFileSync(filename, 'utf8').split(/\r?\n/)) {
    if (!line.trim() || line.trimStart().startsWith('#')) continue;
    const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) throw new Error('Unsupported service environment file syntax.');
    let value = match[2].trim();
    if (value.startsWith('"') || value.startsWith("'")) {
      if (value.at(-1) !== value[0]) throw new Error('Unclosed quote in service environment file.');
      value = value.slice(1, -1);
    }
    values[match[1]] = value;
  }
  return values;
}

export function wrapperConfig(env = process.env, provider) {
  const installed = installation(env);
  const endpoint = new URL(env.OMC_WRAPPER_URL || installed.url || 'http://127.0.0.1:8788');
  if (endpoint.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(endpoint.hostname)
      || endpoint.username || endpoint.password || endpoint.pathname !== '/' || endpoint.search || endpoint.hash) {
    throw new Error('OMC_WRAPPER_URL must be an HTTP origin with a numeric loopback address.');
  }
  const filename = env.OMC_WRAPPER_ENV_FILE || installed.service_env || join(configRoot(env), 'service.env');
  const values = readServiceEnv(filename);
  if (!values.CHAT_API_KEY) throw new Error('The wrapper service environment file has no CHAT_API_KEY.');
  const timeout = Number(env.OMC_WRAPPER_TIMEOUT_SECONDS || values.CHAT_API_TIMEOUT_SECONDS || 300);
  const maximum = Number(values.CHAT_API_MAX_TIMEOUT_SECONDS || 900);
  if (!Number.isInteger(timeout) || timeout < 1 || !Number.isInteger(maximum) || maximum < 1) {
    throw new Error('Wrapper timeouts must be positive integer seconds.');
  }
  return { endpoint, key: values.CHAT_API_KEY, timeout: Math.min(timeout, maximum, 900),
    ...(provider ? requestSelection(provider, env) : {}) };
}

function redact(text, key) {
  return String(text).split(key).join('[REDACTED]');
}

function redactObject(value, key) {
  return JSON.parse(redact(JSON.stringify(value), key));
}

export async function wrapperRequest(config, provider, prompt) {
  if (!['codex', 'claude'].includes(provider)) throw new Error('Unsupported wrapper provider.');
  try {
    const response = await fetch(new URL('/api/chat', config.endpoint), {
      method: 'POST', redirect: 'error',
      headers: { Authorization: `Bearer ${config.key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider, prompt, timeout_seconds: config.timeout,
        ...(config.model === undefined ? {} : { model: config.model }),
        ...(config.effort === undefined ? {} : { effort: config.effort }),
        fallback: false, allow_tools: false, safe_guard: true,
        codex_sandbox: 'read-only', codex_ephemeral: true,
        persist: false, include_raw: false, stream: false,
      }),
      signal: AbortSignal.timeout((config.timeout + 15) * 1000),
    });
    const result = await response.json().catch(() => { throw new Error('Wrapper returned invalid JSON.'); });
    if (!response.ok) {
      const detail = typeof result.error === 'string' ? result.error : result.error?.message || 'request failed';
      const error = new Error(`Wrapper HTTP ${response.status}: ${detail}`);
      error.wrapper = redactObject({
        http_status: response.status, code: result.error?.code || 'wrapper_error',
        retryable: result.error?.retryable ?? false,
        request_id: result.error?.details?.request_id ?? null,
        provider: result.error?.details?.provider ?? provider,
        provider_status: result.error?.details?.provider_status ?? null,
        attempts: result.error?.details?.attempts ?? [],
      }, config.key);
      throw error;
    }
    if (result.provider_status && result.provider_status.outcome !== 'completed') {
      const error = new Error('Wrapper reported an incomplete provider outcome.');
      error.wrapper = redactObject({ code: result.provider_status.reason_code || 'provider_incomplete',
        request_id: result.request_id ?? null, provider, retryable: false, provider_status: result.provider_status }, config.key);
      throw error;
    }
    if (result.provider !== provider || result.exit_code !== 0 || result.dry_run !== false
        || result.fallback?.used || result.fallback?.final || typeof result.text !== 'string' || !result.text.trim()) {
      const error = new Error('Wrapper did not return a successful, nonempty response from the requested provider.');
      error.wrapper = redactObject({ code: 'wrapper_invalid_result', request_id: result.request_id ?? null,
        provider: result.provider ?? provider, retryable: false, provider_status: result.provider_status ?? null }, config.key);
      throw error;
    }
    return { ...result, text: redact(result.text, config.key),
      provider_status: result.provider_status ? redactObject(result.provider_status, config.key) : null };
  } catch (error) {
    const safe = new Error(redact(error.message, config.key).slice(0, 1500));
    if (error.wrapper) safe.wrapper = error.wrapper;
    throw safe;
  }
}

async function writeArtifact(root, provider, originalTask, prompt, result, failure, metadata) {
  const { getOmcRoot } = await import(pathToFileURL(join(root, 'dist/lib/worktree-paths.js')).href);
  const directory = join(getOmcRoot(process.cwd()), 'artifacts', 'ask');
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const slug = originalTask.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'task';
  const timestamp = new Date().toISOString();
  const filename = join(directory, `${provider}-${slug}-${timestamp.replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}.md`);
  const body = [
    `# ${provider} advisor artifact`, '',
    `- Provider: ${provider}`, `- Exit code: ${failure ? 1 : 0}`,
    `- Requested model: ${metadata.requested_model || 'provider default'}`,
    `- Requested effort: ${metadata.requested_effort || 'provider default'}`,
    `- Created at: ${timestamp}`, '- Transport: existing local agent_chat_api wrapper',
    `- Wrapper request ID: ${metadata.request_id || 'unavailable'}`,
    `- Wrapper workspace: ${result?.cwd || 'unavailable'}`, '',
    '## Original task', '', originalTask, '',
    '## Final prompt', '', prompt, '',
    '## Provider status', '', '```json', JSON.stringify(metadata, null, 2), '```', '',
    '## Raw output', '', result?.text || failure, '',
    '## Concise summary', '',
    failure ? `Wrapper request failed: ${failure}` : 'Provider completed through the local wrapper. Review the response before applying it.', '',
    '## Action items', '',
    failure ? '- Resolve the wrapper or provider error and retry.' : '- Extract and verify any decisions before applying changes.', '',
  ].join('\n');
  await writeFile(filename, body, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  await writeFile(filename.replace(/\.md$/, '.json'), JSON.stringify(metadata, null, 2) + '\n', {
    encoding: 'utf8', mode: 0o600, flag: 'wx',
  });
  return filename;
}

async function main(args) {
  if (args[0] === '--check') {
    const config = wrapperConfig();
    const response = await fetch(new URL('/api/readiness', config.endpoint), {
      headers: { Authorization: `Bearer ${config.key}` }, redirect: 'error', signal: AbortSignal.timeout(20000),
    });
    const result = await response.json();
    console.log(JSON.stringify({
      status: response.status, authenticationVerified: result.authentication_verified === true,
      providerStatusSchemaVersion: result.provider_status_schema_version ?? null,
      providers: Object.fromEntries(Object.entries(result.providers || {}).map(([name, value]) => [name, {
        available: value.available, version: value.version, reason: value.reason_code,
      }])),
    }, null, 2));
    if (!response.ok || !result.authentication_verified || !result.providers?.codex?.available || !result.providers?.claude?.available) process.exitCode = 1;
    return;
  }
  const [provider, ...rest] = args;
  if (!provider || !rest.length || !rest.join(' ').trim()) throw new Error('Usage: omc ask <codex|claude> "prompt"');
  const root = pluginRoot();
  if (!['codex', 'claude'].includes(provider)) {
    const child = spawnSync(process.execPath, [join(root, 'scripts/run-provider-advisor.js'), ...args], {
      stdio: 'inherit', env: process.env,
    });
    if (child.error) throw child.error;
    process.exitCode = child.status ?? 1;
    return;
  }
  const prompt = rest.join(' ');
  const originalTask = process.env.OMC_ASK_ORIGINAL_TASK || prompt;
  let result;
  let failure;
  let wrapperError;
  let selected = {};
  try {
    const config = wrapperConfig(process.env, provider);
    selected = { model: config.model, effort: config.effort };
    result = await wrapperRequest(config, provider, prompt);
  } catch (error) {
    failure = error.message;
    wrapperError = error.wrapper;
  }
  const metadata = {
    schema_version: 1, provider, request_id: result?.request_id || wrapperError?.request_id || null,
    requested_model: selected.model || null, requested_effort: selected.effort || null,
    run_id: process.env.OMC_WRAPPER_RUN_ID || null,
    attempts: result?.fallback?.attempts || wrapperError?.attempts || [],
    provider_status: result?.provider_status || wrapperError?.provider_status || {
      schema_version: 1, outcome: failure ? 'failed' : 'completed', requested_model: selected.model || null,
      actual_model: null, actual_model_source: 'unknown', model_switches: [], safety_flags: [],
      block: { detected: null, category: null, source: 'not_exposed' }, visibility: 'not_exposed',
    },
    error: failure ? { code: wrapperError?.code || 'wrapper_error', retryable: wrapperError?.retryable ?? false, message: failure } : null,
  };
  console.log(await writeArtifact(root, provider, originalTask, prompt, result, failure, metadata));
  // OMC's advisor contract keeps stdout as the artifact path. Stderr is also
  // returned to the orchestrator, so status is visible before it reads the file.
  console.error(`[omc-provider-status] ${JSON.stringify(metadata)}`);
  if (failure) {
    console.error(`[ask-${provider}] ${failure}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(error => {
    console.error(`[omc-wrapper] ${error.message}`);
    process.exitCode = 1;
  });
}
