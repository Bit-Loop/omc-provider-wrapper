import { readFileSync, writeFileSync, mkdirSync, mkdtempSync, renameSync, rmSync, readdirSync, statSync } from 'node:fs';
import { spawn, spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { createServer } from 'node:net';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { claudeDir, configRoot, installation, nativeBinaries, openCodeDir } from './installation.mjs';
import { DEFAULT_OLLAMA_URL, isClaudeModel, isOpenCodeModel, loadProviderConfig, permitted, routeSlug } from './provider-config.mjs';

export const MARKER = 'omc-provider-wrapper generated';
const INTEGRATIONS = fileURLToPath(new URL('./integrations/', import.meta.url));
// Local servers ignore the key, but OpenAI-compatible clients still require one.
const PLACEHOLDER_KEY = 'no-key-required';
const WRAPPER_ENV = { default_provider: 'CHAT_API_DEFAULT_PROVIDER', fallback_enabled: 'CHAT_API_PROVIDER_FALLBACK_ENABLED',
  fallback_providers: 'CHAT_API_FALLBACK_PROVIDERS', timeout_seconds: 'CHAT_API_TIMEOUT_SECONDS' };

export function overlayPath(env = process.env, home = homedir()) { return join(openCodeDir(env, home), 'opencode.jsonc'); }
function manifestPath(env, home) { return join(configRoot(env, home), 'generated.json'); }
export function omcCommand(env = process.env, home = homedir()) {
  const { bin_dir: bin } = installation(env, home);
  return bin ? join(bin, 'omc') : 'omc';
}
export function openCodeBinary(env = process.env, home = homedir()) { return nativeBinaries(env, home).opencode ?? null; }

function atomicWrite(filename, content) {
  mkdirSync(dirname(filename), { recursive: true });
  const temporary = `${filename}.${randomUUID()}.tmp`;
  writeFileSync(temporary, content, { mode: 0o600, flag: 'wx' });
  renameSync(temporary, filename);
}

function readOptional(filename) {
  try { return readFileSync(filename, 'utf8'); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function freePort() {
  return new Promise((done, fail) => {
    const server = createServer();
    server.on('error', fail);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => done(port));
    });
  });
}

// A private server with an empty config sees every catalog model. The installed
// overlay would hide the models it disables, so it cannot be its own source.
export async function openCodeCatalog(binary, env = process.env, timeoutMs = 25000) {
  const scratch = mkdtempSync(join(tmpdir(), 'omc-opencode-catalog-'));
  const password = randomUUID();
  const port = await freePort();
  const { OPENCODE_CONFIG_DIR: _dir, OPENCODE_CONFIG: _file, OPENCODE_CONFIG_CONTENT: _content, ...inherited } = env;
  const child = spawn(binary, ['serve', '--port', String(port), '--hostname', '127.0.0.1'], { cwd: scratch, stdio: 'ignore',
    env: { ...inherited, XDG_CONFIG_HOME: scratch, OPENCODE_SERVER_PASSWORD: password, OPENCODE_NO_AUTOLLAMA: '1', OPENCODE_DISABLE_AUTOUPDATE: '1' } });
  const exited = new Promise(done => child.once('exit', done));
  try {
    const url = `http://127.0.0.1:${port}/api/model?directory=${encodeURIComponent(scratch)}`;
    const headers = { Authorization: `Basic ${Buffer.from(`opencode:${password}`).toString('base64')}` };
    const deadline = Date.now() + timeoutMs;
    let previous = -1;
    while (Date.now() < deadline) {
      if (child.exitCode !== null) throw new Error('OpenCode exited before listing its catalog.');
      try {
        const response = await fetch(url, { headers, signal: AbortSignal.timeout(3000) });
        const models = response.ok ? (await response.json()).data ?? [] : [];
        // Providers load independently; only a repeated nonzero count is complete.
        if (models.length && models.length === previous) return models.map(model => `${model.providerID}/${model.id}`);
        previous = models.length;
      } catch { /* Still starting. */ }
      await new Promise(done => setTimeout(done, 400));
    }
    throw new Error('Timed out listing the OpenCode model catalog.');
  } finally {
    child.kill('SIGTERM');
    await exited;
    rmSync(scratch, { recursive: true, force: true });
  }
}

async function liveApiModels(api, env) {
  const key = api.key_env ? env[api.key_env] : undefined;
  try {
    const response = await fetch(`${api.base_url}/models`, { redirect: 'error', signal: AbortSignal.timeout(3000),
      headers: key ? { Authorization: `Bearer ${key}` } : {} });
    if (!response.ok) return null;
    const ids = ((await response.json()).data ?? []).map(model => model.id).filter(id => typeof id === 'string');
    return ids.length ? ids : null;
  } catch { return null; }
}

function isBuiltinOllama(id, api) { return id === 'ollama' && api.base_url === DEFAULT_OLLAMA_URL && !api.models; }

// OpenCode discovers the default local Ollama itself. Everything else needs its
// model list; an unreachable endpoint keeps the models it last reported.
export async function resolveApiModels(config, previous = {}, env = process.env) {
  const resolved = {};
  for (const [id, api] of Object.entries(config.apis)) {
    if (isBuiltinOllama(id, api)) continue;
    resolved[id] = api.models ?? await liveApiModels(api, env)
      ?? Object.keys(previous.providers?.[id]?.models ?? {}).filter(model => !previous.providers[id].models[model].disabled);
  }
  return resolved;
}

function configuredModels(directory) {
  const content = readOptional(join(directory, 'opencode.json'));
  if (!content) return [];
  let document;
  try { document = JSON.parse(content); } catch { return []; }
  return Object.entries({ ...document.provider, ...document.providers })
    .flatMap(([id, provider]) => Object.keys(provider?.models ?? {}).map(model => `${id}/${model}`));
}

function splitModel(reference) {
  const [base, variant] = reference.split('#');
  const slash = base.indexOf('/');
  return { providerID: base.slice(0, slash), model: base.slice(slash + 1), ...(variant ? { variant } : {}) };
}

export function renderOverlay(config, { catalog = [], apiModels = {} } = {}) {
  const overlay = { $schema: 'https://opencode.ai/config.json' };
  if (config.opencode.default_model) overlay.model = config.opencode.default_model;
  // enabled_providers is the only provider gate OpenCode 2 enforces; `policies` is not.
  if (config.opencode.apis) overlay.enabled_providers = config.opencode.apis;
  const providers = {};
  for (const [id, models] of Object.entries(apiModels)) {
    const api = config.apis[id];
    providers[id] = { name: api.name || id, package: 'aisdk:@ai-sdk/openai-compatible',
      settings: { baseURL: api.base_url, apiKey: api.key_env ? `{env:${api.key_env}}` : PLACEHOLDER_KEY },
      models: Object.fromEntries(models.map(model => [model, { name: model,
        capabilities: { tools: true, input: ['text'], output: ['text'] } }])) };
  }
  // OpenCode 2 ignores whitelist/blacklist and wildcards, so an allow-list has to
  // disable every other model individually.
  for (const reference of new Set([...catalog, ...Object.entries(apiModels).flatMap(([id, models]) => models.map(model => `${id}/${model}`))])) {
    if (permitted(reference, config.opencode.allowed_models)) continue;
    const { providerID, model } = splitModel(reference);
    providers[providerID] ??= {};
    providers[providerID].models ??= {};
    providers[providerID].models[model] = { ...providers[providerID].models[model], disabled: true };
  }
  if (Object.keys(providers).length) overlay.providers = providers;
  const agents = Object.fromEntries(config.routes.filter(route => isOpenCodeModel(route.model)).map(route => [`route-${routeSlug(route.task)}`, {
    model: splitModel(route.model), mode: 'subagent',
    description: `ROUTE: always delegate work on ${route.task} to this agent. It runs on ${route.model}.`,
  }]));
  if (Object.keys(agents).length) overlay.agents = agents;
  return overlay;
}

export function renderClaudeRoute(route, omc = 'omc') {
  const name = `omc-route-${routeSlug(route.task)}`;
  const claude = isClaudeModel(route.model);
  const description = `MUST BE USED for ${route.task}. Runs on ${route.model}.`;
  const body = claude ? `You handle this kind of work: ${route.task}. Complete the delegated task.\n`
    : `Send the delegated task to the model \`${route.model}\` and return its answer. Do not answer it yourself.\n\n`
      + `Run the following, with the full delegated task as the heredoc body:\n\n`
      + "```bash\n" + `${shellQuote(omc)} ask opencode --model '${route.model}' --prompt "$(cat <<'OMC_ROUTE_TASK'\nTASK\nOMC_ROUTE_TASK\n)"\n` + "```\n\n"
      + 'Stdout is an artifact path. Read that file and return its "Raw output" section. If the command fails, report the failure.\n';
  return ['---', `name: ${name}`, `description: ${JSON.stringify(description)}`, `model: ${claude ? route.model : 'haiku'}`,
    ...(claude ? [] : ['tools: Bash, Read']), '---', `<!-- ${MARKER} -->`, body].join('\n');
}

function shellQuote(value) { return `'${value.replaceAll("'", "'\\''")}'`; }

function integrationFiles(directory, target) {
  const root = join(INTEGRATIONS, directory);
  return readdirSync(root, { recursive: true }).map(String).filter(name => statSync(join(root, name)).isFile())
    .map(name => ({ source: join(root, name), target: join(target, name) }));
}

export function renderFiles(config, overlay, env = process.env, home = homedir()) {
  const omc = omcCommand(env, home);
  const files = { [overlayPath(env, home)]: `// ${MARKER}: change it with \`omc provider\`; your settings live in providers.json.\n${JSON.stringify(overlay, null, 2)}\n` };
  const openCode = openCodeDir(env, home);
  const claude = claudeDir(env, home);
  for (const { source, target } of [...integrationFiles('opencode', openCode), ...integrationFiles('claude', claude)]) {
    files[target] = readFileSync(source, 'utf8').replaceAll('{{OMC}}', shellQuote(omc));
  }
  for (const route of config.routes) {
    files[join(claude, 'agents', `omc-route-${routeSlug(route.task)}.md`)] = renderClaudeRoute(route, omc);
  }
  return files;
}

function validateConfig(config, catalog) {
  const { default_model: model, allowed_models: allowed } = config.opencode;
  if (model && !permitted(model, allowed)) throw new Error(`The default model ${model} is not in the permitted models. Permit it or choose another default.`);
  for (const route of config.routes) {
    if (isOpenCodeModel(route.model) && !permitted(route.model, allowed)) throw new Error(`Route "${route.task}" uses ${route.model}, which is not permitted.`);
  }
  for (const id of config.hermes.api ? [config.hermes.api] : []) {
    if (!config.apis[id]) throw new Error(`Hermes uses unknown API ${id}.`);
  }
  if (allowed && catalog) {
    const unmatched = allowed.filter(pattern => !catalog.some(reference => permitted(reference, [pattern])));
    if (unmatched.length) return [`No model in the catalog matches: ${unmatched.join(', ')}`];
  }
  return [];
}

export function hermesSteps(config) {
  const { model, api, provider } = config.hermes;
  const steps = [];
  if (model) steps.push(['set', 'model.default', model]);
  if (provider) steps.push(['set', 'model.provider', provider]);
  if (api) {
    const definition = config.apis[api];
    steps.push(['set', `providers.${api}.base_url`, definition.base_url], ['set', 'model.provider', api], ['unset', 'model.base_url']);
    // Hermes resolves key_env from its own .env file and never interpolates api_key.
    if (definition.key_env) steps.push(['set', `providers.${api}.key_env`, definition.key_env]);
  }
  return steps;
}

function hermesBinary(env) { return env.OMC_WRAPPER_HERMES_BIN || 'hermes'; }

export function applyHermes(config, env = process.env) {
  const profile = config.hermes.profile ? ['-p', config.hermes.profile] : [];
  for (const [action, ...setting] of hermesSteps(config)) {
    const result = spawnSync(hermesBinary(env), [...profile, 'config', action, ...(action === 'set' ? ['--force'] : []), ...setting], { encoding: 'utf8', timeout: 30000, env });
    if (action === 'unset' && /not set/i.test(`${result.stdout}${result.stderr}`)) continue;
    if (result.error || result.status !== 0) {
      const detail = result.error?.message || `${result.stdout}\n${result.stderr}`.split('\n').filter(line => line && !/SyntaxWarning|return ip/.test(line)).pop();
      throw new Error(`hermes config ${action} ${setting[0]} failed: ${detail || `exit ${result.status}`}`);
    }
  }
}

export function renderServiceEnv(content, wrapper) {
  const values = Object.entries(WRAPPER_ENV).filter(([key]) => wrapper[key] !== undefined)
    .map(([key, name]) => [name, Array.isArray(wrapper[key]) ? wrapper[key].join(',') : String(wrapper[key])]);
  const lines = content.split('\n');
  for (const [name, value] of values) {
    const line = `${name}="${value}"`;
    const index = lines.findIndex(item => item.startsWith(`${name}=`));
    if (index >= 0) lines[index] = line;
    else lines.splice(lines.at(-1) === '' ? lines.length - 1 : lines.length, 0, line);
  }
  return lines.join('\n');
}

function applyWrapper(config, env, home, report) {
  const file = installation(env, home).service_env;
  const current = file ? readOptional(file) : null;
  if (current === null) {
    if (Object.keys(config.wrapper).length) report.warnings.push('Wrapper settings wait for install.sh; no service.env exists yet.');
    return;
  }
  const next = renderServiceEnv(current, config.wrapper);
  if (next === current) return;
  atomicWrite(file, next);
  report.wrapper_restarted = spawnSync('systemctl', ['--user', 'try-restart', 'omc-provider-wrapper.service'], { stdio: 'ignore', timeout: 20000 }).status === 0;
}

function hash(value) { return createHash('sha256').update(JSON.stringify(value)).digest('hex'); }

export async function applyProviderConfig({ env = process.env, home = homedir(), dryRun = false, reload = true, catalogLoader = openCodeCatalog } = {}) {
  const config = loadProviderConfig(env, home);
  const report = { overlay: overlayPath(env, home), written: [], removed: [], warnings: [], hermes_applied: false };
  const overlayText = readOptional(report.overlay);
  if (overlayText !== null && !overlayText.includes(MARKER)) {
    throw new Error(`${report.overlay} is your own file. Move its settings into opencode.json so the wrapper can own that path.`);
  }
  let previous = {};
  try { previous = overlayText ? JSON.parse(overlayText.replace(/^\/\/.*\n/, '')) : {}; } catch { /* Regenerated below. */ }
  const binary = openCodeBinary(env, home);
  let catalog;
  if (config.opencode.allowed_models) {
    if (!binary) throw new Error('A permitted-model list needs the OpenCode CLI, which was not found.');
    catalog = [...await catalogLoader(binary, env), ...configuredModels(openCodeDir(env, home))];
  }
  const apiModels = await resolveApiModels(config, previous, env);
  for (const [id, models] of Object.entries(apiModels)) {
    if (!models.length) report.warnings.push(`API ${id} is unreachable and has no known models. Pass --models or start it, then re-apply.`);
  }
  report.warnings.push(...validateConfig(config, catalog && [...catalog, ...Object.entries(apiModels).flatMap(([id, models]) => models.map(model => `${id}/${model}`))]));
  const overlay = renderOverlay(config, { catalog, apiModels });
  const files = renderFiles(config, overlay, env, home);
  const manifestFile = manifestPath(env, home);
  let manifest = {};
  try { manifest = JSON.parse(readOptional(manifestFile) ?? '{}'); } catch { /* Rebuilt below. */ }
  // Hermes runs first: it is the step that can fail on outside state, and a
  // failure here must leave every rendered file matching the settings kept.
  const hermesHash = hash(hermesSteps(config).concat([config.hermes.profile ?? '']));
  if (hermesSteps(config).length && manifest.hermes !== hermesHash) {
    report.hermes_applied = true;
    if (!dryRun) applyHermes(config, env);
  }
  for (const [filename, content] of Object.entries(files)) {
    const existing = readOptional(filename);
    if (existing === content) continue;
    if (existing !== null && !existing.includes(MARKER)) {
      report.warnings.push(`Left your file in place: ${filename}`);
      delete files[filename];
      continue;
    }
    report.written.push(filename);
    if (!dryRun) atomicWrite(filename, content);
  }
  for (const stale of (manifest.files ?? []).filter(name => !(name in files))) {
    if (!readOptional(stale)?.includes(MARKER)) continue;
    report.removed.push(stale);
    if (!dryRun) rmSync(stale);
  }
  if (!dryRun) {
    applyWrapper(config, env, home, report);
    atomicWrite(manifestFile, JSON.stringify({ schema_version: 1, files: Object.keys(files), hermes: hermesSteps(config).length ? hermesHash : null }, null, 2) + '\n');
    if (reload && binary && report.written.includes(report.overlay)) {
      spawnSync(binary, ['reload'], { stdio: 'ignore', timeout: 8000, env: { ...env, OPENCODE_NO_AUTOLLAMA: '1' } });
    }
  }
  return report;
}

export function relativeTo(home, filename) {
  const path = relative(home, filename);
  return path.startsWith('..') ? filename : `~/${path}`;
}
