import { readFileSync, mkdirSync, writeFileSync, renameSync, chmodSync, symlinkSync, readdirSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve, delimiter } from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { claudeDir, nativeBinaries, configRoot, openCodeDir } from './installation.mjs';
import { loadProviderConfig, saveProviderConfig } from './provider-config.mjs';

export const PROVIDERS = ['claude', 'codex', 'opencode'];
// OpenCode's default belongs to providers.json, which renders it into OpenCode.
const FILE_PROVIDERS = ['claude', 'codex'];
// Selected when nothing configures OpenCode, so it keeps its own choice.
export const OPENCODE_NATIVE_DEFAULT = 'default';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function nativeStateRoot(env = process.env, home = homedir()) {
  return resolve(env.XDG_STATE_HOME || join(home, '.local/state'), 'omc-provider-wrapper');
}

export function selector(value, label = 'Model') {
  if (typeof value !== 'string' || !value.trim() || value.length > 1024 || /[\0\r\n]/.test(value)) {
    throw new Error(`${label} must contain 1–1024 characters and no NUL or newline characters.`);
  }
  return value;
}

export function modelConfigPath(env = process.env, home = homedir()) {
  return resolve(env.OMC_WRAPPER_MODEL_CONFIG || join(configRoot(env, home), 'models.json'));
}

function readOptional(filename) {
  try { return readFileSync(filename, 'utf8'); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function tomlModels(content) {
  const found = [];
  for (const line of (content || '').split(/\r?\n/)) {
    const match = line.match(/^\s*model\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#.*)?$/);
    if (match) found.push(match[1][0] === '"' ? JSON.parse(match[1]) : match[1].slice(1, -1));
  }
  return found;
}

function nativeConfigFiles(env, home) {
  const codexHome = env.CODEX_HOME || join(home, '.codex');
  return {
    claude: join(claudeDir(env, home), 'settings.json'),
    codex: join(codexHome === '~' ? home : codexHome.startsWith('~/') ? join(home, codexHome.slice(2)) : resolve(codexHome), 'config.toml'),
    opencode: join(openCodeDir(env, home), 'opencode.json'),
  };
}

function openCodeNativeModel(content) {
  let model;
  try { model = JSON.parse(content || '{}').model; } catch { return undefined; }
  return model?.providerID ? `${model.providerID}/${model.model}` : model;
}

export function nativeModels(home = homedir(), env = process.env) {
  const files = nativeConfigFiles(env, home);
  const claudeText = readOptional(files.claude);
  const codexText = readOptional(files.codex);
  const claude = claudeText ? JSON.parse(claudeText).model : undefined;
  // Only the root table sets the native default; profile models are listed separately.
  const codex = tomlModels((codexText || '').split(/^\s*\[/m)[0])[0];
  return { claude: selector(claude || 'opus'), codex: selector(codex || 'gpt-6-sol'),
    opencode: selector(openCodeNativeModel(readOptional(files.opencode)) || OPENCODE_NATIVE_DEFAULT) };
}

export function savedModels(env = process.env, home = homedir()) {
  const defaults = nativeModels(home, env);
  const opencode = selector(loadProviderConfig(env, home).opencode.default_model ?? defaults.opencode);
  const content = readOptional(modelConfigPath(env, home));
  if (!content) return { ...defaults, opencode };
  const document = JSON.parse(content);
  const models = document.models || document;
  return { ...Object.fromEntries(FILE_PROVIDERS.map(provider => [provider, selector(models[provider] ?? defaults[provider])])), opencode };
}

export function saveModels(models, env = process.env, home = homedir()) {
  const filename = modelConfigPath(env, home);
  const validated = Object.fromEntries(FILE_PROVIDERS.map(provider => [provider, selector(models[provider])]));
  // Saving another provider's model must not pin OpenCode to what it inherits today.
  if (models.opencode !== undefined && models.opencode !== savedModels(env, home).opencode) {
    const config = loadProviderConfig(env, home);
    saveProviderConfig({ ...config, opencode: { ...config.opencode, default_model: selector(models.opencode) } }, env, home);
  }
  mkdirSync(dirname(filename), { recursive: true, mode: 0o700 });
  chmodSync(dirname(filename), 0o700);
  const temporary = `${filename}.${randomUUID()}.tmp`;
  writeFileSync(temporary, JSON.stringify({ schema_version: 1, models: validated }, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  renameSync(temporary, filename);
  return { ...validated, opencode: savedModels(env, home).opencode };
}

export function readRunProfile(env = process.env) {
  if (!env.OMC_WRAPPER_RUN_FILE) return null;
  const run = JSON.parse(readFileSync(env.OMC_WRAPPER_RUN_FILE, 'utf8'));
  if (run.schema_version !== 1 || !UUID.test(run.run_id) || !run.models) throw new Error('Invalid OMC wrapper run profile.');
  for (const provider of FILE_PROVIDERS) selector(run.models[provider]);
  return run;
}

export function selectedModels(env = process.env, home = homedir()) {
  const saved = savedModels(env, home);
  const run = readRunProfile(env);
  const native = {
    claude: env.ANTHROPIC_MODEL ?? env.CLAUDE_MODEL,
    codex: env.OMC_CODEX_DEFAULT_MODEL ?? env.OMC_EXTERNAL_MODELS_DEFAULT_CODEX_MODEL,
  };
  return Object.fromEntries(PROVIDERS.map(provider => [provider,
    selector(env[`OMC_WRAPPER_${provider.toUpperCase()}_MODEL`] ?? run?.models[provider] ?? native[provider] ?? saved[provider]),
  ]));
}

export function requestSelection(provider, env = process.env, home = homedir()) {
  if (!PROVIDERS.includes(provider)) throw new Error('Unsupported wrapper provider.');
  const model = selector(env.OMC_WRAPPER_REQUEST_MODEL ?? selectedModels(env, home)[provider]);
  const effort = env.OMC_WRAPPER_REQUEST_EFFORT;
  return { ...(provider === 'opencode' && model === OPENCODE_NATIVE_DEFAULT ? {} : { model }), ...(effort === undefined ? {} : { effort: selector(effort, 'Effort') }) };
}

export function knownModels(env = process.env, home = homedir()) {
  const saved = savedModels(env, home);
  const selected = selectedModels(env, home);
  const files = nativeConfigFiles(env, home);
  const claudeText = readOptional(files.claude);
  const configuredClaude = Object.keys((claudeText ? JSON.parse(claudeText).modelSettings : null) || {});
  return {
    claude: [...new Set(['opus', 'sonnet', 'haiku', ...configuredClaude, saved.claude, selected.claude])],
    codex: [...new Set([...tomlModels(readOptional(files.codex)), saved.codex, selected.codex])],
    opencode: [...new Set([...(loadProviderConfig(env, home).opencode.allowed_models ?? []), saved.opencode, selected.opencode])],
  };
}

function flagValue(args, index, flag) {
  const token = args[index];
  if (token.startsWith(`${flag}=`)) return { value: selector(token.slice(flag.length + 1)), count: 1 };
  if (token === flag) {
    if (args[index + 1] === undefined || args[index + 1].startsWith('--')) throw new Error(`Missing value for ${flag}.`);
    return { value: selector(args[index + 1]), count: 2 };
  }
  return null;
}

// Wrapper selectors occupy a leading option block, so upstream option operands
// and prompt text can never be mistaken for wrapper flags.
export function extractSelectors(args) {
  const output = [];
  const models = {};
  let index = 0;
  let requestModel, effort;
  function providerSelector() {
    for (const name of PROVIDERS) {
      const value = flagValue(args, index, `--${name}-model`);
      if (!value) continue;
      models[name] = value.value;
      index += value.count;
      return true;
    }
    return false;
  }
  // Every OMC command accepts wrapper selectors before its command name.
  while (index < args.length && providerSelector()) {}
  const command = args[index];
  if (command === 'ask' && PROVIDERS.includes(args[index + 1])) {
    output.push(command, args[index + 1]);
    index += 2;
    while (index < args.length) {
      if (providerSelector()) continue;
      const model = flagValue(args, index, '--model');
      const level = flagValue(args, index, '--effort');
      if (model || level) {
        if (model) requestModel = model.value;
        else effort = selector(level.value, 'Effort');
        index += (model || level).count;
        continue;
      }
      const token = args[index];
      if (token === '--agent-prompt') {
        output.push(token);
        index++;
        if (args[index] !== undefined) output.push(args[index++]);
        continue;
      }
      if (token.startsWith('--agent-prompt=')) { output.push(token); index++; continue; }
      // OMC's ask parser understands --prompt, but treats -- as prompt text.
      if (token === '--') { output.push('--prompt'); index++; }
      break;
    }
  } else if (command === 'launch' || !command || command.startsWith('-')) {
    if (command === 'launch') { output.push(command); index++; }
    // Native model flags keep their original spelling and position. The first
    // other native option ends the wrapper block, including all of its operands.
    while (index < args.length) {
      if (providerSelector()) continue;
      if (['--yolo', '--madmax'].includes(args[index])) { output.push(args[index++]); continue; }
      const value = flagValue(args, index, '--model') || flagValue(args, index, '-m');
      if (!value) break;
      models.claude = value.value;
      output.push(...args.slice(index, index + value.count));
      index += value.count;
    }
  }
  output.push(...args.slice(index));
  return { args: output, models, requestModel, effort };
}

export function createRunProfile(models, env = process.env, options = {}) {
  const home = options.home || homedir();
  const launchId = randomUUID();
  const inherited = readRunProfile(env);
  const runId = UUID.test(env.OMC_WRAPPER_RUN_ID || '') ? env.OMC_WRAPPER_RUN_ID : randomUUID();
  const directory = join(options.runRoot || join(nativeStateRoot(env, home), 'launches'), launchId);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
  const run = {
    schema_version: 1, run_id: runId, launch_id: launchId,
    workspace: resolve(options.cwd || process.cwd()),
    models: { ...Object.fromEntries(FILE_PROVIDERS.map(provider => [provider, selector(models[provider])])),
      opencode: selector(models.opencode ?? OPENCODE_NATIVE_DEFAULT) },
    native_binaries: options.nativeBinaries || nativeBinaries(env, home, inherited?.native_binaries),
    state_dir: resolve(env.OMC_WRAPPER_STATE_DIR || inherited?.state_dir || join(nativeStateRoot(env, home), 'native')),
    provider_path: directory,
  };
  const filename = join(directory, 'run.json');
  writeFileSync(filename, JSON.stringify(run, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  for (const provider of Object.keys(run.native_binaries)) symlinkSync(options.providerShim || fileURLToPath(new URL('./provider-cli.sh', import.meta.url)), join(directory, provider));
  return { run, filename, env: { ...env, PATH: [directory, env.PATH || ''].join(delimiter),
    OMC_WRAPPER_RUN_FILE: filename, OMC_WRAPPER_RUN_ID: runId, OMC_WRAPPER_STATE_DIR: run.state_dir } };
}

export function wrapperStatus(env = process.env, home = homedir()) {
  const run = readRunProfile(env);
  const stateDir = resolve(env.OMC_WRAPPER_STATE_DIR || run?.state_dir || join(nativeStateRoot(env, home), 'native'));
  const runId = env.OMC_WRAPPER_RUN_ID;
  if (runId && !UUID.test(runId)) throw new Error('OMC_WRAPPER_RUN_ID must be a UUID.');
  const root = join(stateDir, 'runs', ...(runId ? [runId] : []));
  const files = [];
  function walk(directory) {
    let entries;
    try { entries = readdirSync(directory, { withFileTypes: true }); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
    for (const entry of entries) {
      const filename = join(directory, entry.name);
      if (entry.isDirectory()) walk(filename);
      else if (entry.isFile() && entry.name.endsWith('.json') && dirname(filename).endsWith('/jobs')) {
        files.push({ filename, mtime: statSync(filename).mtimeMs });
      }
    }
  }
  walk(root);
  const errors = [];
  const jobs = files.sort((a, b) => b.mtime - a.mtime).slice(0, 200).flatMap(({ filename }) => {
    try { return [{ ...JSON.parse(readFileSync(filename, 'utf8')), status_file: filename }]; }
    catch { errors.push({ status_file: filename, error: 'Invalid job state JSON.' }); return []; }
  });
  return { schema_version: 1, state_dir: stateDir, run_id: runId || null, jobs, errors, truncated: files.length > 200 };
}
