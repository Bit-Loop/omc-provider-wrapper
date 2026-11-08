import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { configRoot, installation, openCodeDir } from './installation.mjs';
import { savedModels } from './models.mjs';
import { applyProviderConfig, MARKER, openCodeBinary, openCodeCatalog, overlayPath, relativeTo, renderOverlay, resolveApiModels } from './provider-apply.mjs';
import { apiId, baseUrl, DEFAULT_OLLAMA_URL, isClaudeModel, keyEnv, loadProviderConfig, modelPattern, permitted, providerConfigPath, routeModel, routeSlug, saveProviderConfig } from './provider-config.mjs';

const USAGE = `omc provider                     Show all provider settings (--json for raw)
omc provider doctor              Check every target, and that OpenCode still enforces them
omc provider apply [--dry-run]   Re-render everything from providers.json
omc provider update              Pull the wrapper source, reinstall, re-apply
omc provider model [MODEL]       Show or set the default OpenCode model
omc provider models [FILTER]     List permitted OpenCode models
omc provider allow <list|set|add|remove|all> [MODEL...]   Permitted OpenCode models ('*' wildcards)
omc provider route <list|add "TASK" MODEL|remove "TASK">  "For TASK use MODEL"
omc provider api <list|add ID URL [--key-env VAR] [--models a,b] [--name N]|remove ID>
omc provider opencode [--apis a,b|all]
omc provider hermes [--profile P] [--model M] [--api ID|--provider NAME]
omc provider ollama [--url URL]
omc provider wrapper [--default-provider P] [--fallback a,b] [--fallback-enabled true|false] [--timeout SECONDS]`;

function flags(args, known) {
  const values = {};
  const rest = [];
  for (let index = 0; index < args.length; index++) {
    const token = args[index];
    if (!token.startsWith('--')) { rest.push(token); continue; }
    const [name, inline] = token.slice(2).split(/=(.*)/s);
    if (!known.includes(name)) throw new Error(`Unknown option --${name}.`);
    const value = inline ?? args[++index];
    if (value === undefined) throw new Error(`Missing value for --${name}.`);
    values[name] = value;
  }
  return { values, rest };
}

function list(value) { return value.split(',').map(item => item.trim()).filter(Boolean); }

async function allModels(config, { env, home, options }) {
  const binary = openCodeBinary(env, home);
  if (!binary) throw new Error('The OpenCode CLI was not found.');
  const apiModels = await resolveApiModels(config, {}, env);
  const catalog = options.catalogLoader ?? openCodeCatalog;
  return [...new Set([...await catalog(binary, env),
    ...Object.entries(apiModels).flatMap(([id, models]) => models.map(model => `${id}/${model}`)),
    ...configuredProviderModels(env, home)])].sort();
}

function configuredProviderModels(env, home) {
  try {
    const document = JSON.parse(readFileSync(join(openCodeDir(env, home), 'opencode.json'), 'utf8'));
    return Object.entries({ ...document.provider, ...document.providers })
      .flatMap(([id, provider]) => Object.keys(provider?.models ?? {}).map(model => `${id}/${model}`));
  } catch { return []; }
}

export function resolveModel(query, models) {
  if (models.includes(query.split('#')[0])) return query;
  const needle = query.toLowerCase();
  const matches = models.filter(model => model.toLowerCase().includes(needle));
  if (matches.length === 1) return matches[0];
  if (!matches.length) throw new Error(`No permitted model matches "${query}". Try: omc provider models`);
  throw new Error(`"${query}" matches ${matches.length} models:\n  ${matches.slice(0, 20).join('\n  ')}`);
}

function ollamaUrl(config) { return config.apis.ollama?.base_url ?? DEFAULT_OLLAMA_URL; }

function summary(report, home) {
  const lines = [...report.written.map(file => `updated ${relativeTo(home, file)}`), ...report.removed.map(file => `removed ${relativeTo(home, file)}`)];
  if (report.hermes_applied) lines.push('updated Hermes');
  if (report.wrapper_restarted) lines.push('restarted the wrapper service');
  lines.push(...report.warnings.map(warning => `warning: ${warning}`));
  return lines.join('\n');
}

function show(config, env, home) {
  const model = savedModels(env, home).opencode;
  const allowed = config.opencode.allowed_models;
  const hermes = config.hermes;
  const wrapper = config.wrapper;
  return [`Settings: ${relativeTo(home, providerConfigPath(env, home))}`,
    `OpenCode  default ${model}`,
    `          APIs ${config.opencode.apis?.join(', ') ?? 'all'}`,
    `          permitted ${allowed?.join(', ') ?? 'all models'}`,
    `Routes    ${config.routes.length ? '' : 'none'}`,
    ...config.routes.map(route => `          ${route.task} -> ${route.model}`),
    `APIs      ${Object.keys(config.apis).length ? '' : 'none'}`,
    ...Object.entries(config.apis).map(([id, api]) => `          ${id}  ${api.base_url}${api.key_env ? `  key $${api.key_env}` : ''}`),
    `Hermes    ${hermes.model || hermes.api || hermes.provider ? `profile ${hermes.profile ?? 'active'}, model ${hermes.model ?? 'unchanged'}, API ${hermes.api ?? hermes.provider ?? 'unchanged'}` : 'unmanaged'}`,
    `Ollama    ${ollamaUrl(config)}`,
    `Wrapper   ${Object.keys(wrapper).length ? JSON.stringify(wrapper) : 'install defaults'}`].join('\n');
}

// A setting that cannot be applied is not kept. Otherwise one bad value would
// fail every later command, since each one re-applies the whole file.
async function save(config, env, home, options) {
  const previous = loadProviderConfig(env, home);
  const saved = saveProviderConfig(config, env, home);
  try {
    return { saved, report: await applyProviderConfig({ env, home, ...options }) };
  } catch (error) {
    saveProviderConfig(previous, env, home);
    throw error;
  }
}

async function setPermitted(config, patterns, context) {
  const { env, home, options, print } = context;
  const next = { ...config, opencode: { ...config.opencode, allowed_models: patterns } };
  if (patterns && config.opencode.default_model && !permitted(config.opencode.default_model, patterns)) {
    throw new Error(`That would exclude the default model ${config.opencode.default_model}. Change the default first.`);
  }
  const { report } = await save(next, env, home, options);
  print(`Permitted OpenCode models: ${patterns?.join(', ') ?? 'all'}`);
  const text = summary(report, home);
  if (text) print(text);
}

async function allow(args, config, context) {
  const [action = 'list', ...models] = args;
  const current = config.opencode.allowed_models;
  if (action === 'list' && !models.length) return context.print(current ? current.join('\n') : 'All models are permitted.');
  if (action === 'all' && !models.length) return setPermitted(config, null, context);
  if (!models.length) throw new Error('Usage: omc provider allow <list|set|add|remove|all> [MODEL...]');
  models.forEach(modelPattern);
  if (action === 'set') return setPermitted(config, [...new Set(models)], context);
  if (action === 'add') return setPermitted(config, [...new Set([...(current ?? []), ...models])], context);
  if (action === 'remove') {
    if (!current) throw new Error('All models are permitted; use `allow set` to start a list.');
    const next = current.filter(model => !models.includes(model));
    if (!next.length) throw new Error('That would permit no models. Use `allow all` to lift the list.');
    return setPermitted(config, next, context);
  }
  throw new Error('Usage: omc provider allow <list|set|add|remove|all> [MODEL...]');
}

async function route(args, config, context) {
  const { env, home, options, print } = context;
  const [action = 'list', task, model] = args;
  if (action === 'list' && !task) return print(config.routes.length ? config.routes.map(item => `${item.task} -> ${item.model}`).join('\n') : 'No routes.');
  const slug = task ? routeSlug(task) : null;
  const others = config.routes.filter(item => routeSlug(item.task) !== slug);
  if (action === 'add' && task?.trim() && model && args.length === 3) {
    routeModel(model);
    if (!isClaudeModel(model) && !permitted(model, config.opencode.allowed_models)) throw new Error(`${model} is not a permitted model.`);
    const { report } = await save({ ...config, routes: [...others, { task: task.trim(), model }] }, env, home, options);
    print(`Route saved: ${task.trim()} -> ${model}`);
    const text = summary(report, home);
    if (text) print(text);
    return;
  }
  if (action === 'remove' && task && args.length === 2) {
    if (others.length === config.routes.length) throw new Error(`No route for "${task}".`);
    const { report } = await save({ ...config, routes: others }, env, home, options);
    print(`Route removed: ${task}`);
    const text = summary(report, home);
    if (text) print(text);
    return;
  }
  throw new Error('Usage: omc provider route <list|add "TASK" MODEL|remove "TASK">');
}

async function api(args, config, context) {
  const { env, home, options, print } = context;
  const [action = 'list', ...rest] = args;
  if (action === 'list' && !rest.length) {
    return print(Object.entries(config.apis).map(([id, item]) => `${id}  ${item.base_url}${item.key_env ? `  key $${item.key_env}` : ''}`).join('\n') || 'No APIs.');
  }
  if (action === 'add') {
    const { values, rest: positional } = flags(rest, ['key-env', 'models', 'name']);
    if (positional.length !== 2) throw new Error('Usage: omc provider api add ID URL [--key-env VAR] [--models a,b] [--name NAME]');
    const [id, url] = positional;
    const entry = { base_url: baseUrl(url), ...(values['key-env'] ? { key_env: keyEnv(values['key-env']) } : {}),
      ...(values.models ? { models: list(values.models) } : {}), ...(values.name ? { name: values.name } : {}) };
    const { report } = await save({ ...config, apis: { ...config.apis, [apiId(id)]: entry } }, env, home, options);
    print(`API ${id}: ${entry.base_url}`);
    const text = summary(report, home);
    if (text) print(text);
    return;
  }
  if (action === 'remove' && rest.length === 1) {
    const [id] = rest;
    if (!config.apis[id]) throw new Error(`Unknown API ${id}.`);
    if (config.hermes.api === id) throw new Error(`Hermes uses API ${id}; move it first.`);
    const { [id]: _removed, ...apis } = config.apis;
    const { report } = await save({ ...config, apis }, env, home, options);
    print(`API ${id} removed.`);
    const text = summary(report, home);
    if (text) print(text);
    return;
  }
  throw new Error('Usage: omc provider api <list|add ID URL ...|remove ID>');
}

async function targetSettings(target, args, config, context) {
  const { env, home, options, print } = context;
  let next;
  if (target === 'opencode') {
    const { values, rest } = flags(args, ['apis']);
    if (rest.length) throw new Error('Usage: omc provider opencode [--apis a,b|all]');
    if (values.apis) next = { ...config, opencode: { ...config.opencode, apis: values.apis === 'all' ? null : list(values.apis).map(apiId) } };
  } else if (target === 'hermes') {
    const { values, rest } = flags(args, ['profile', 'model', 'api', 'provider']);
    if (rest.length || (values.api && values.provider)) throw new Error('Usage: omc provider hermes [--profile P] [--model M] [--api ID|--provider NAME]');
    if (values.api && !config.apis[values.api]) throw new Error(`Unknown API ${values.api}. Add it with: omc provider api add`);
    if (Object.keys(values).length) {
      const { api: _api, provider: _provider, ...kept } = config.hermes;
      next = { ...config, hermes: { ...(values.api || values.provider ? kept : config.hermes), ...values } };
    }
  } else if (target === 'ollama') {
    const { values, rest } = flags(args, ['url']);
    if (rest.length) throw new Error('Usage: omc provider ollama [--url URL]');
    if (values.url) next = { ...config, apis: { ...config.apis, ollama: { ...config.apis.ollama, base_url: baseUrl(values.url) } } };
  } else {
    const { values, rest } = flags(args, ['default-provider', 'fallback', 'fallback-enabled', 'timeout']);
    if (rest.length) throw new Error('Usage: omc provider wrapper [--default-provider P] [--fallback a,b] [--fallback-enabled true|false] [--timeout SECONDS]');
    if (values['fallback-enabled'] && !['true', 'false'].includes(values['fallback-enabled'])) throw new Error('--fallback-enabled takes true or false.');
    if (values.timeout && !/^[1-9][0-9]*$/.test(values.timeout)) throw new Error('--timeout takes whole seconds.');
    if (Object.keys(values).length) {
      next = { ...config, wrapper: { ...config.wrapper,
        ...(values['default-provider'] ? { default_provider: values['default-provider'] } : {}),
        ...(values.fallback ? { fallback_providers: list(values.fallback) } : {}),
        ...(values['fallback-enabled'] ? { fallback_enabled: values['fallback-enabled'] } : {}),
        ...(values.timeout ? { timeout_seconds: Number(values.timeout) } : {}) } };
    }
  }
  if (next) {
    const { saved, report } = await save(next, env, home, options);
    config = saved;
    const text = summary(report, home);
    if (text) print(text);
  }
  if (target === 'ollama') {
    print(`Ollama ${ollamaUrl(config)}`);
    const models = await ollamaModels(config);
    print(models ? `Reachable; models: ${models.join(', ') || 'none'}` : 'Not reachable.');
  } else print(JSON.stringify(target === 'opencode' ? config.opencode : config[target], null, 2));
}

async function ollamaModels(config) {
  try {
    const response = await fetch(`${ollamaUrl(config).replace(/\/v1$/, '')}/api/tags`, { signal: AbortSignal.timeout(3000) });
    return response.ok ? ((await response.json()).models ?? []).map(model => model.name) : null;
  } catch { return null; }
}

async function setDefaultModel(query, config, context) {
  const { env, home, options, print } = context;
  const candidates = (await allModels(config, context)).filter(model => permitted(model, config.opencode.allowed_models));
  const model = resolveModel(query, candidates);
  const { report } = await save({ ...config, opencode: { ...config.opencode, default_model: model } }, env, home, options);
  print(`OpenCode default model: ${model}\nNew sessions use it. Switch the current session with /models.`);
  const text = summary(report, home);
  if (text) print(text);
}

export function splitSlashArguments(text) {
  const tokens = [];
  const pattern = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g;
  for (const match of text.matchAll(pattern)) tokens.push(match[1]?.replace(/\\(.)/g, '$1') ?? match[2] ?? match[3]);
  return tokens;
}

const SUBCOMMANDS = new Set(['show', 'doctor', 'apply', 'model', 'models', 'allow', 'route', 'api', 'opencode', 'hermes', 'ollama', 'wrapper']);

// Slash commands take free text. Anything that is not a subcommand goes back to
// the model, which handles requests like "for reviews use glm" through the skill.
function slashCommand(kind, text) {
  const tokens = splitSlashArguments(text);
  if (kind === 'model') {
    if (tokens.length > 1) return null;
    return ['model', ...tokens];
  }
  if (!tokens.length) return ['show'];
  return SUBCOMMANDS.has(tokens[0]) ? tokens : null;
}

async function readAll(stream) {
  let text = '';
  for await (const chunk of stream) text += chunk;
  return text;
}

function probe(binary, args, env, options = {}) {
  return spawnSync(binary, args, { encoding: 'utf8', timeout: 45000, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...env, OPENCODE_NO_AUTOLLAMA: '1', ...options.env }, cwd: options.cwd });
}

// The behaviors below come from OpenCode internals that have changed before, so
// doctor proves them with a provider that can never reach a real model.
function enforcementWorks(binary, env) {
  const models = { allowed: {}, blocked: { disabled: true } };
  const content = JSON.stringify({ providers: { 'omc-probe': { name: 'probe', package: 'aisdk:@ai-sdk/openai-compatible',
    settings: { baseURL: 'http://127.0.0.1:9/v1', apiKey: 'probe' },
    models: Object.fromEntries(Object.entries(models).map(([id, extra]) => [id, { name: id, capabilities: { tools: false, input: ['text'], output: ['text'] }, ...extra }])) } } });
  const result = probe(binary, ['run', '--standalone', '--format', 'json', '-m', 'omc-probe/blocked', 'probe'], env, { env: { OPENCODE_CONFIG_CONTENT: content } });
  return /provider\.no-route|Model unavailable/.test(result.stdout);
}

async function doctor(config, context) {
  const { env, home, print } = context;
  let failed = false;
  const check = (ok, label, detail = '') => {
    failed ||= ok === false;
    print(`${ok === false ? 'FAIL' : ok === null ? 'note' : 'ok  '}  ${label}${detail ? ` — ${detail}` : ''}`);
  };
  const binary = openCodeBinary(env, home);
  check(binary ? true : null, 'OpenCode CLI', binary ?? 'not installed');
  if (binary) {
    const version = probe(binary, ['--version'], env).stdout?.match(/\d+\.\d+\.\d+/)?.[0];
    const proven = dependencyVersion();
    check(true, 'OpenCode version', (version ?? 'unknown') + (proven && version !== proven ? ` (last proven on ${proven}; the checks below cover the difference)` : ''));
    let overlay = null;
    try { overlay = readFileSync(overlayPath(env, home), 'utf8'); } catch { /* Reported below. */ }
    check(overlay?.includes(MARKER) ?? false, 'Overlay installed', relativeTo(home, overlayPath(env, home)));
    if (overlay) {
      const sources = probe(binary, ['debug', 'config'], env).stdout || '';
      const [user, managed] = [sources.indexOf('opencode.json"'), sources.indexOf('opencode.jsonc"')];
      check(managed >= 0 ? user < managed : null, 'Overlay loads after opencode.json', managed >= 0 ? '' : 'OpenCode did not list its sources');
    }
    check(enforcementWorks(binary, env), 'OpenCode enforces disabled models');
    if (config.opencode.allowed_models && overlay) {
      try {
        const expected = renderOverlay(config, { catalog: await openCodeCatalog(binary, env), apiModels: await resolveApiModels(config, {}, env) });
        const installed = JSON.parse(overlay.replace(/^\/\/.*\n/, ''));
        const missing = Object.entries(expected.providers ?? {}).flatMap(([id, provider]) => Object.keys(provider.models ?? {})
          .filter(model => provider.models[model].disabled && !installed.providers?.[id]?.models?.[model]?.disabled).map(model => `${id}/${model}`));
        check(missing.length === 0, 'Allow-list covers the catalog', missing.length ? `${missing.length} new models are unrestricted; run omc provider apply` : '');
      } catch (error) { check(null, 'Allow-list coverage', error.message); }
    }
  }
  const models = await ollamaModels(config);
  check(models ? true : null, 'Ollama', models ? `${ollamaUrl(config)}, ${models.length} models` : `${ollamaUrl(config)} not reachable`);
  const installed = installation(env, home);
  check(installed.root ? true : null, 'Wrapper install', installed.root ?? 'not installed; run ./install.sh');
  if (installed.url) {
    // /health probes each provider, so it can take seconds, and 503 still means it is running.
    const status = await fetch(new URL('/health', installed.url), { signal: AbortSignal.timeout(20000) }).then(response => response.status, () => null);
    check(status === 200 ? true : null, 'Wrapper service', status === null ? `${installed.url} not responding` : status === 200 ? installed.url : `${installed.url} running, but a provider is not ready; see omc wrapper status`);
  }
  if (Object.keys(config.hermes).length) {
    const profile = config.hermes.profile ? ['-p', config.hermes.profile] : [];
    const result = spawnSync(env.OMC_WRAPPER_HERMES_BIN || 'hermes', [...profile, 'config', 'get', 'model'], { encoding: 'utf8', timeout: 30000, env });
    const ok = result.status === 0 && (!config.hermes.model || result.stdout.includes(config.hermes.model));
    check(ok, 'Hermes matches settings', ok ? '' : 'run omc provider apply');
  }
  if (failed) process.exitCode = 1;
}

function dependencyVersion() {
  try { return JSON.parse(readFileSync(new URL('../dependencies.json', import.meta.url), 'utf8')).providers?.opencode_proven; }
  catch { return undefined; }
}

function update(args, context) {
  const { env, home, print } = context;
  const installed = installation(env, home);
  // Everything else belongs to install.sh, so it passes through unparsed.
  const at = args.indexOf('--source');
  const rest = at < 0 ? args : args.toSpliced(at, 2);
  if (at >= 0 && (!args[at + 1] || args[at + 1].startsWith('--'))) throw new Error('--source needs a directory.');
  const source = (at < 0 ? undefined : args[at + 1]) ?? installed.source_dir;
  if (!source) throw new Error('No source checkout is recorded. Use --source DIR, or run install.sh from your clone.');
  const git = (...command) => spawnSync('git', ['-C', source, ...command], { encoding: 'utf8' });
  if (git('rev-parse', '--git-dir').status === 0) {
    if (git('status', '--porcelain').stdout.trim()) throw new Error(`${source} has uncommitted changes. Commit or stash them first.`);
    if (git('rev-parse', '--abbrev-ref', '@{upstream}').status === 0) {
      const pull = git('pull', '--ff-only');
      if (pull.status !== 0) throw new Error(`git pull failed: ${pull.stderr.trim()}`);
      print(pull.stdout.trim());
    } else print('No upstream branch; installing the local checkout.');
  }
  // install.sh ends by re-applying providers.json, which lives outside what it replaces.
  const recorded = { OMC_WRAPPER_INSTALL_DIR: installed.root, OMC_WRAPPER_BIN_DIR: installed.bin_dir,
    CLAUDE_CONFIG_DIR: installed.claude_config_dir, OPENCODE_CONFIG_DIR: installed.opencode_config_dir,
    XDG_CONFIG_HOME: installed.xdg_config_home, XDG_STATE_HOME: installed.xdg_state_home };
  const installEnv = { ...Object.fromEntries(Object.entries(recorded).filter(([, value]) => value)), ...env,
    OMC_WRAPPER_CONFIG_DIR: configRoot(env, home) };
  const install = spawnSync(join(source, 'install.sh'), rest, { stdio: 'inherit', env: installEnv });
  if (install.error || install.status !== 0) throw new Error('install.sh failed; your settings are unchanged. Run omc provider doctor.');
}

export async function providerCommand(args, { env = process.env, home = homedir(), print = console.log, options = {} } = {}) {
  const context = { env, home, print, options };
  const [command = 'show', ...rest] = args;
  if (command === 'slash') {
    const [kind] = rest;
    const text = (await readAll(process.stdin)).trim();
    const dispatch = slashCommand(kind, text);
    if (!dispatch) return print(`NATURAL_LANGUAGE: ${text}`);
    try { return await providerCommand(dispatch, { env, home, print, options }); }
    catch (error) { process.exitCode = 0; return print(`Failed: ${error.message}`); }
  }
  const config = loadProviderConfig(env, home);
  switch (command) {
    case 'help': case '--help': case '-h': return print(USAGE);
    case 'show':
      if (rest.length > 1 || (rest[0] && rest[0] !== '--json')) throw new Error('Usage: omc provider show [--json]');
      return print(rest[0] ? JSON.stringify({ ...config, opencode: { ...config.opencode, effective_model: savedModels(env, home).opencode } }, null, 2) : show(config, env, home));
    case 'apply': {
      if (rest.length > 1 || (rest[0] && rest[0] !== '--dry-run')) throw new Error('Usage: omc provider apply [--dry-run]');
      const report = await applyProviderConfig({ env, home, ...options, dryRun: rest[0] === '--dry-run' });
      return print(summary(report, home) || 'Already up to date.');
    }
    case 'doctor': return doctor(config, context);
    case 'update': return update(rest, context);
    case 'model':
      if (rest.length > 1) throw new Error('Usage: omc provider model [MODEL]');
      return rest[0] ? setDefaultModel(rest[0], config, context) : print(`${savedModels(env, home).opencode}\nPermitted: ${config.opencode.allowed_models?.join(', ') ?? 'all models'}`);
    case 'models': {
      if (rest.length > 1) throw new Error('Usage: omc provider models [FILTER]');
      const needle = rest[0]?.toLowerCase();
      const models = (await allModels(config, context)).filter(model => permitted(model, config.opencode.allowed_models) && (!needle || model.toLowerCase().includes(needle)));
      return print(models.join('\n') || 'No matching models.');
    }
    case 'allow': return allow(rest, config, context);
    case 'route': return route(rest, config, context);
    case 'api': return api(rest, config, context);
    case 'opencode': case 'hermes': case 'ollama': case 'wrapper': return targetSettings(command, rest, config, context);
    default: throw new Error(`Unknown provider command: ${command}\n\n${USAGE}`);
  }
}
