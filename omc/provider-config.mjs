import { readFileSync, mkdirSync, writeFileSync, renameSync, chmodSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { configRoot } from './installation.mjs';

// User intent lives here, outside the install directory, so reinstalling or
// updating the wrapper can never lose it. Every other file is rendered from it.
export const SCHEMA_VERSION = 1;
export const DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434/v1';
const API_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
// provider/model[#variant]. Stricter than OpenCode's grammar: references reach
// shell lines in generated agents, so quotes and expansions must not parse.
const MODEL_REF = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._:@+/-]+(?:#[A-Za-z0-9._-]+)?$/;
const MODEL_PATTERN = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._:@+/*-]+$/;
const CLAUDE_MODEL = /^(?:opus|sonnet|haiku|fable|claude-[A-Za-z0-9._-]+)(?:\[[A-Za-z0-9]+\])?$/;
const WRAPPER_PROVIDERS = ['claude', 'codex', 'opencode'];

export function providerConfigPath(env = process.env, home = homedir()) {
  return resolve(env.OMC_WRAPPER_PROVIDER_CONFIG || join(configRoot(env, home), 'providers.json'));
}

export function apiId(value) {
  if (typeof value !== 'string' || !API_ID.test(value)) throw new Error(`Invalid API id: ${value}. Use lowercase letters, digits, dot, dash or underscore.`);
  return value;
}

export function baseUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error(`Invalid API URL: ${value}`); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('API URLs must be http(s) without embedded credentials. Use --key-env for keys.');
  }
  return String(value).replace(/\/+$/, '');
}

export function keyEnv(value) {
  if (typeof value !== 'string' || !ENV_NAME.test(value)) throw new Error('--key-env takes an environment variable name, never the key itself.');
  return value;
}

export function isOpenCodeModel(value) { return typeof value === 'string' && MODEL_REF.test(value); }
export function isClaudeModel(value) { return typeof value === 'string' && CLAUDE_MODEL.test(value); }

export function openCodeModel(value) {
  if (!isOpenCodeModel(value)) throw new Error(`OpenCode models are provider/model, got: ${value}`);
  return value;
}

export function modelPattern(value) {
  if (typeof value !== 'string' || !MODEL_PATTERN.test(value)) throw new Error(`Permitted models are provider/model, with * as a wildcard, got: ${value}`);
  return value;
}

export function routeModel(value) {
  if (!isOpenCodeModel(value) && !isClaudeModel(value)) throw new Error(`Route models are provider/model or a Claude model, got: ${value}`);
  return value;
}

function object(value, label) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error(`providers.json: ${label} must be an object.`);
  return value;
}

function strings(value, label) {
  if (value === null || value === undefined) return null;
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string' || !item)) throw new Error(`providers.json: ${label} must be a list of strings or null.`);
  return value;
}

function normalize(document) {
  object(document, 'document');
  if (Number(document.schema_version) > SCHEMA_VERSION) {
    throw new Error(`providers.json uses schema ${document.schema_version}; this wrapper reads ${SCHEMA_VERSION}. Update the wrapper.`);
  }
  const apis = object(document.apis ?? { ollama: { base_url: DEFAULT_OLLAMA_URL } }, 'apis');
  for (const [id, api] of Object.entries(apis)) {
    apiId(id);
    object(api, `apis.${id}`);
    baseUrl(api.base_url);
    if (api.key_env !== undefined) keyEnv(api.key_env);
    strings(api.models, `apis.${id}.models`);
  }
  const opencode = object(document.opencode ?? {}, 'opencode');
  if (opencode.default_model) openCodeModel(opencode.default_model);
  strings(opencode.allowed_models, 'opencode.allowed_models')?.forEach(modelPattern);
  const routes = document.routes ?? [];
  if (!Array.isArray(routes)) throw new Error('providers.json: routes must be a list.');
  for (const route of routes) {
    object(route, 'route');
    if (typeof route.task !== 'string' || !route.task.trim()) throw new Error('providers.json: each route needs a task.');
    routeModel(route.model);
  }
  const wrapper = object(document.wrapper ?? {}, 'wrapper');
  if (wrapper.default_provider !== undefined && !WRAPPER_PROVIDERS.includes(wrapper.default_provider)) {
    throw new Error(`providers.json: wrapper.default_provider must be one of ${WRAPPER_PROVIDERS.join(', ')}.`);
  }
  // Unknown keys survive, so an older wrapper keeps a newer one's settings.
  return { ...document, schema_version: SCHEMA_VERSION, apis, routes, wrapper,
    opencode: { ...opencode, default_model: opencode.default_model ?? null,
      apis: strings(opencode.apis, 'opencode.apis'), allowed_models: strings(opencode.allowed_models, 'opencode.allowed_models') },
    hermes: object(document.hermes ?? {}, 'hermes') };
}

export function loadProviderConfig(env = process.env, home = homedir()) {
  let content;
  try { content = readFileSync(providerConfigPath(env, home), 'utf8'); }
  catch (error) { if (error.code === 'ENOENT') return normalize({}); throw error; }
  return normalize(JSON.parse(content));
}

export function saveProviderConfig(config, env = process.env, home = homedir()) {
  const filename = providerConfigPath(env, home);
  const validated = normalize(config);
  mkdirSync(dirname(filename), { recursive: true, mode: 0o700 });
  chmodSync(dirname(filename), 0o700);
  const temporary = `${filename}.${randomUUID()}.tmp`;
  writeFileSync(temporary, JSON.stringify(validated, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  renameSync(temporary, filename);
  return validated;
}

export function routeSlug(task) {
  return task.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48).replace(/-+$/, '') || 'task';
}

// `*` is the only wildcard, so a pasted model id can never act as a pattern by accident.
export function permitted(model, patterns) {
  if (!patterns) return true;
  const base = model.split('#')[0];
  return patterns.some(pattern => new RegExp(`^${pattern.split('*').map(part => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')}$`).test(base));
}
