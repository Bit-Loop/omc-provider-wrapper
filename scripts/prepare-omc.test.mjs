import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, readdirSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { runInNewContext } from 'node:vm';
import { pluginRoot } from '../omc/runtime.mjs';

const prepare = fileURLToPath(new URL('./prepare-omc.mjs', import.meta.url));
const files = ['dist/team/tmux-session.js', 'bridge/cli.cjs', 'bridge/runtime-cli.cjs', 'bridge/team-mcp.cjs', 'bridge/team.js'];
const patchedFiles = [...files, 'dist/team/runtime-v2.js'];

function fixture(t) {
  const source = pluginRoot(), root = mkdtempSync(join(tmpdir(), 'omc-prepare-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const dir of ['native', 'bridge', 'dist/team', 'dist/installer', 'scripts/lib', 'hooks']) mkdirSync(join(root, dir), { recursive: true });
  // Share dependencies, but copy every file that preparation can mutate.
  for (const entry of readdirSync(join(source, 'dist'))) {
    if (!['team', 'installer'].includes(entry)) symlinkSync(join(source, 'dist', entry), join(root, 'dist', entry));
  }
  for (const entry of readdirSync(join(source, 'dist/team'))) {
    if (!['tmux-session.js', 'runtime-v2.js'].includes(entry)) symlinkSync(join(source, 'dist/team', entry), join(root, 'dist/team', entry));
  }
  for (const file of patchedFiles) {
    let text = readFileSync(join(source, file), 'utf8');
    // Exercise a pristine pinned payload even when the local install is patched.
    text = text.replace(/(['"])command\1, (shellEscape\(['"]env['"]\),\n)/g, '$2');
    text = text.replace(/^\s*['"]-x['"], ['"]200['"], ['"]-y['"], String\(Math.max\(24, (?:workerCount|\(options.layoutWorkerCount \?\? workerCount\)) \* 12\)\),\n/gm, '');
    text = text.replace(/^\s*layoutWorkerCount: config\d*\.workerCount,\n/gm, '');
    text = text.replace(/^\s*await applyMainVerticalLayout\(teamTarget, \{ required: true, tmuxServerIdentity \}\); \/\/ wrapper: reflow each split\n/gm, '');
    writeFileSync(join(root, file), text);
  }
  for (const file of ['native/contained-fs.c', 'package.json', 'package-lock.json', 'hooks/hooks.json']) {
    writeFileSync(join(root, file), readFileSync(join(source, file)));
  }
  // The skill/hook compaction helpers are unrelated to team patching.
  writeFileSync(join(root, 'dist/installer/index.js'), 'export function compactPluginSkillPayload() {}');
  writeFileSync(join(root, 'scripts/lib/hook-command-normalizer.mjs'), 'export const normalizeHooksDataForPlatform = x => x;');
  return root;
}

function apply(root) {
  const result = spawnSync(process.execPath, [prepare, root], { encoding: 'utf8', timeout: 15000 });
  assert.equal(result.status, 0, result.stderr);
}

test('preparation sizes every actual bundled team entry point and is idempotent', t => {
  const root = fixture(t);
  // An unrelated marker must not be mistaken for a patched v2 call site.
  const runtime = join(root, 'dist/team/runtime-v2.js');
  writeFileSync(runtime, readFileSync(runtime, 'utf8') + '\n// layoutWorkerCount: unrelated comment\n');
  apply(root);
  const once = patchedFiles.map(file => readFileSync(join(root, file), 'utf8'));
  for (const [index, text] of once.slice(0, files.length).entries()) {
    const declaration = text.match(/const detachedArgs = \[[\s\S]*?\];/)[0];
    const args = runInNewContext(`${declaration}; detachedArgs`, {
      detachedFormat: 'FORMAT', workerCount: 0, options: { layoutWorkerCount: 5 }, detachedSessionName: 'fixture',
      cwd: root, cwd2: root, detachedPaneShell: { args: [] },
    });
    assert.ok(args.includes('-x'), `${files[index]} still creates an 80x24 team`);
    assert.equal(args[args.indexOf('-x') + 1], '200');
    assert.equal(args[args.indexOf('-y') + 1], '60');
    assert.match(text, /['"]command['"], shellEscape\(['"]env['"]\)/);
  }
  for (const [index, text] of once.slice(0, files.length).entries()) assert.ok(/workerPaneIds.push\(splitRecord.paneId\);\s*await applyMainVerticalLayout\(teamTarget, \{ required: true, tmuxServerIdentity \}\); \/\/ wrapper: reflow each split/.test(text), `${files[index]} must reflow between splits`);
  for (const file of ['dist/team/runtime-v2.js', 'bridge/cli.cjs', 'bridge/runtime-cli.cjs', 'bridge/team.js']) assert.ok(/createTeamSession\(sanitized, 0, leaderCwd, \{\n\s*layoutWorkerCount: config\d*\.workerCount,/.test(readFileSync(join(root, file), 'utf8')), `${file} must pass the final v2 worker count`);
  apply(root);
  assert.deepEqual(patchedFiles.map(file => readFileSync(join(root, file), 'utf8')), once);
});

test('unknown pinned team code fails before any team entry point is modified', t => {
  const root = fixture(t);
  writeFileSync(join(root, files[3]), readFileSync(join(root, files[3]), 'utf8').replace('const detachedArgs = [', 'const changedArgs = [' ).replace(/      detachedFormat,\n/, '      changedFormat,\n'));
  const before = patchedFiles.map(file => readFileSync(join(root, file), 'utf8'));
  const result = spawnSync(process.execPath, [prepare, root], { encoding: 'utf8', timeout: 15000 });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not match the detached team size patch/);
  assert.deepEqual(patchedFiles.map(file => readFileSync(join(root, file), 'utf8')), before);
});

for (const dynamic of [false, true]) test(`prepared real tmux session creates ten usable panes (${dynamic ? 'v2 leader-only start' : 'preallocated start'})`, { timeout: 60000 }, t => {
  const root = fixture(t);
  apply(root);
  const tmuxDir = join(root, 'tmux');
  mkdirSync(tmuxDir, { mode: 0o700 });
  const code = `
    import { createTeamSession, killTeamSession, splitTeamWorkerPaneWithEvidence, applyMainVerticalLayout } from ${JSON.stringify(pathToFileURL(join(root, files[0])).href)};
    import { execFileSync } from 'node:child_process';
    const team = await createTeamSession('wrapper-geometry', ${dynamic ? 0 : 10}, ${JSON.stringify(root)}, {layoutWorkerCount:10});
    try {
      if (${dynamic}) for (let i = 0; i < 10; i++) {
        const split = await splitTeamWorkerPaneWithEvidence(i ? team.workerPaneIds.at(-1) : team.leaderPaneId,
          i ? 'down' : 'right', ${JSON.stringify(root)}, 'tmux', team.tmuxServerIdentity);
        if (!split.commandSucceeded) throw Error(split.stderr);
        team.workerPaneIds.push(split.paneId);
        await applyMainVerticalLayout(team.sessionName, {required:true, tmuxServerIdentity:team.tmuxServerIdentity});
      }
      console.log(JSON.stringify(team.workerPaneIds.map(pane => execFileSync('tmux',
        ['-S', team.tmuxServerIdentity.socket_path, 'display-message', '-p', '-t', pane, '#{pane_width} #{pane_height}'],
        {encoding:'utf8'}).trim().split(' ').map(Number))));
    } finally {
      await killTeamSession(team.sessionName.split(':')[0], team.workerPaneIds, team.leaderPaneId, team);
    }
  `;
  const env = { ...process.env, TMUX_TMPDIR: tmuxDir, OMC_RUNTIME_CLI_PATH: join(root, 'bridge/runtime-cli.cjs') };
  for (const key of ['TMUX', 'TMUX_PANE', 'CMUX_WORKSPACE_ID', 'CMUX_SURFACE_ID']) delete env[key];
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', code], { env, encoding: 'utf8', timeout: 50000 });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const sizes = JSON.parse(result.stdout.trim().split('\n').at(-1));
  assert.equal(sizes.length, 10);
  for (const [width, height] of sizes) assert.ok(width >= 80 && height >= 10, `unusable pane: ${width}x${height}`);
});
