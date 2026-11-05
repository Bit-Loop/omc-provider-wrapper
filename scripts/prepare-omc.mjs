#!/usr/bin/env node
// Reproduce the upstream install-time changes in a pinned local OMC payload.
import { readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = resolve(process.argv[2]);
// patches/omc-contained-fs-linux.patch: macOS-only variables otherwise break
// upstream's Linux -Werror build. Match the pinned source exactly; be idempotent.
const nativeFile = join(root, 'native/contained-fs.c');
const before = 'static napi_value process_start_time(napi_env env, napi_callback_info info) {\n  napi_value args[1], result, value;\n  pid_t pid;\n  if (!arguments(env, info, 1, args) || !process_pid(env, args[0], &pid)) return NULL;\n#ifdef __APPLE__';
const after = before.replace('args[1], result, value;', 'args[1];').replace('#ifdef __APPLE__', '#ifdef __APPLE__\n  napi_value result, value;');
const native = readFileSync(nativeFile, 'utf8');
if (native.includes(before)) writeFileSync(nativeFile, native.replace(before, after));
else if (!native.includes(after)) throw new Error('Pinned OMC native source does not match the bundled Linux patch.');
const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
const sqlite = lock.packages['node_modules/better-sqlite3'].version;
manifest.allowScripts = { [`better-sqlite3@${sqlite}`]: true };
writeFileSync(join(root, 'package.json'), JSON.stringify(manifest, null, 2) + '\n');
const { compactPluginSkillPayload } = await import(pathToFileURL(join(root, 'dist/installer/index.js')).href);
compactPluginSkillPayload(root);
const { normalizeHooksDataForPlatform } = await import(pathToFileURL(join(root, 'scripts/lib/hook-command-normalizer.mjs')).href);
const hooks = join(root, 'hooks/hooks.json');
writeFileSync(hooks, JSON.stringify(normalizeHooksDataForPlatform(JSON.parse(readFileSync(hooks, 'utf8'))), null, 2) + '\n');
