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
// Team workers start as `env VAR=... shell ...` typed into an interactive pane.
// A shell function named env (grc's zsh integration defines one) runs even when
// the word is quoted, and grc pipes stdout, so the provider gets no terminal.
// `command` skips functions and aliases in sh, bash, zsh and fish. Patch the
// distributed module AND each self-contained bundle: the CLI/runtime/MCP do not
// import the module, so patching only dist leaves the actual launch unchanged.
const teamPatches = [];
for (const [file, quote] of [['dist/team/tmux-session.js', "'"], ['bridge/cli.cjs', '"'], ['bridge/runtime-cli.cjs', '"'], ['bridge/team-mcp.cjs', '"'], ['bridge/team.js', '"']]) {
  const path = join(root, file);
  const launch = `shellEscape(${quote}env${quote}),\n`;
  let text = readFileSync(path, 'utf8');
  if (!text.includes(`${quote}command${quote}, ${launch}`)) {
    if (text.split(launch).length !== 2) throw new Error(`Pinned OMC ${file} does not match the worker launch patch.`);
    text = text.replace(launch, `${quote}command${quote}, ${launch}`);
  }
  // Detached teams otherwise inherit tmux's 80x24 default. Five workers then
  // receive 39x4 panes, too short for Claude's prompt/readiness evidence.
  const detached = quote === "'" ? "'new-session', '-d', '-P', '-F', detachedFormat,\n" : '      detachedFormat,\n';
  const indent = quote === "'" ? '            ' : '      ';
  // v2 initially creates a leader-only session (workerCount=0), then adds
  // workers. Its caller supplies the final count separately for layout sizing.
  const sizeArgs = `${indent}${quote}-x${quote}, ${quote}200${quote}, ${quote}-y${quote}, String(Math.max(24, (options.layoutWorkerCount ?? workerCount) * 12)),\n`;
  const sized = `${detached}${sizeArgs}`;
  const legacySized = `${detached}${indent}${quote}-x${quote}, ${quote}200${quote}, ${quote}-y${quote}, String(Math.max(24, workerCount * 12)),\n`;
  text = text.replace(legacySized, detached);
  if (!text.includes(sized)) {
    if (text.split(detached).length !== 2) throw new Error(`Pinned OMC ${file} does not match the detached team size patch.`);
    text = text.replace(detached, sized);
  }
  // Repeatedly halving the last pane can exhaust it before the final layout,
  // even when the window has ample space. Rebalance after every tracked split,
  // inside upstream's existing ownership/rollback boundary.
  const split = `${indent}workerPaneIds.push(splitRecord.paneId);\n`;
  const reflow = `${split}${indent}await applyMainVerticalLayout(teamTarget, { required: true, tmuxServerIdentity }); // wrapper: reflow each split\n`;
  if (!text.includes(reflow)) {
    if (text.split(split).length !== 2) throw new Error(`Pinned OMC ${file} does not match the team split patch.`);
    text = text.replace(split, reflow);
  }
  teamPatches.push([path, text]);
}
// v2 is bundled into the CLI and runtime owner; the MCP bundle uses the other
// team path. Preserve the leader-only allocation and upstream ownership checks.
for (const file of ['dist/team/runtime-v2.js', 'bridge/cli.cjs', 'bridge/runtime-cli.cjs', 'bridge/team.js']) {
  const path = join(root, file);
  const staged = teamPatches.find(item => item[0] === path);
  let text = staged ? staged[1] : readFileSync(path, 'utf8');
  const call = /createTeamSession\(sanitized, 0, leaderCwd, \{\n(\s*)newWindow: Boolean\((config\d*)\.newWindow\)/g;
  const matches = [...text.matchAll(call)];
  const patched = /createTeamSession\(sanitized, 0, leaderCwd, \{\n\s*layoutWorkerCount: (config\d*)\.workerCount,\n\s*newWindow: Boolean\(\1\.newWindow\)/g;
  const patchedMatches = [...text.matchAll(patched)];
  if (matches.length === 1 && patchedMatches.length === 0) {
    text = text.replace(call, (_, indent, config) => `createTeamSession(sanitized, 0, leaderCwd, {\n${indent}layoutWorkerCount: ${config}.workerCount,\n${indent}newWindow: Boolean(${config}.newWindow)`);
  } else if (matches.length !== 0 || patchedMatches.length !== 1) throw new Error(`Pinned OMC ${file} does not match the v2 layout count patch.`);
  if (staged) staged[1] = text;
  else teamPatches.push([path, text]);
}
// Validate all entry points before changing any of them.
for (const [path, text] of teamPatches) writeFileSync(path, text);
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
