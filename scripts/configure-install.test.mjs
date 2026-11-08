import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, statSync, readdirSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('configuration preserves user hooks and credentials on reinstall and backs up a replaced launcher', t => {
  const dir = mkdtempSync(join(tmpdir(), 'omc-configure-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const root = join(dir, 'app with spaces'), cfg = join(dir, 'config'), bin = join(dir, 'bin'), claude = join(dir, 'claude');
  for (const path of [root, cfg, bin, claude]) mkdirSync(path);
  writeFileSync(join(claude, 'settings.json'), JSON.stringify({ model: 'custom', hooks: { PostToolUse: [{ hooks: [{ type: 'command', command: 'existing-user-hook' }] }] } }));
  writeFileSync(join(bin, 'omc'), '#!/bin/sh\necho previous\n');
  const args = [fileURLToPath(new URL('./configure-install.mjs', import.meta.url)), root, cfg, bin, '/fixture/claude', '/fixture/codex', '/fixture/python', claude, '18788'];
  const env = { ...process.env, HOME: dir, XDG_CONFIG_HOME: join(dir, 'xdg-config'), XDG_STATE_HOME: join(dir, 'state') };
  const first = spawnSync(process.execPath, args, { env, encoding: 'utf8' });
  assert.equal(first.status, 0, first.stderr);
  const credential = readFileSync(join(cfg, 'service.env'), 'utf8');
  assert.equal(statSync(join(cfg, 'service.env')).mode & 0o777, 0o600);
  const second = spawnSync(process.execPath, [...args.slice(0, -1), '19888'], { env, encoding: 'utf8' });
  assert.equal(second.status, 0, second.stderr);
  assert.equal(readFileSync(join(cfg, 'service.env'), 'utf8'), credential);
  const installed = JSON.parse(readFileSync(join(cfg, 'install.json'), 'utf8'));
  assert.equal(installed.url, 'http://127.0.0.1:18788');
  assert.equal(installed.root, root);
  assert.equal(installed.bin_dir, bin);
  assert.equal(installed.claude_config_dir, claude);
  assert.equal(installed.xdg_state_home, env.XDG_STATE_HOME);
  assert.ok(credential.includes(`OMC_WRAPPER_CONFIG_DIR="${cfg}"`));
  const settings = JSON.parse(readFileSync(join(claude, 'settings.json'), 'utf8'));
  assert.equal(settings.model, 'custom');
  assert.equal(settings.hooks.PostToolUse.length, 2);
  assert.equal(settings.hooks.PostToolUse[0].hooks[0].command, 'existing-user-hook');
  assert.equal(settings.hooks.UserPromptSubmit.length, 1);
  assert.equal(readdirSync(bin).filter(name => name.startsWith('omc.before-')).length, 1);
  assert.match(readFileSync(join(bin, 'omc'), 'utf8'), /# omc-provider-wrapper launcher/);
});

test('optional provider paths cannot inject service environment lines', t => {
  const directory = mkdtempSync(join(tmpdir(), 'omc-configure-paths-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const cfg = join(directory, 'config');
  const args = [fileURLToPath(new URL('./configure-install.mjs', import.meta.url)), join(directory, 'app'), cfg,
    join(directory, 'bin'), '/fixture/claude', '/fixture/codex', '/fixture/python', join(directory, 'claude'), '18788'];
  for (const name of ['OPENCODE_CONFIG_DIR', 'OMC_WRAPPER_PROVIDER_CONFIG']) {
    for (const value of ['relative/path', '/fixture/"quoted', '/fixture/\\escaped', '/fixture/line\ninjected=yes']) {
      const result = spawnSync(process.execPath, args, { encoding: 'utf8', env: {
        HOME: directory, PATH: '/usr/bin:/bin', XDG_STATE_HOME: join(directory, 'state'), [name]: value,
      } });
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, new RegExp(`${name} must be an absolute path`));
      assert.equal(existsSync(cfg), false, 'invalid paths fail before changing configuration');
    }
  }
  const opencode = join(directory, "user's OpenCode");
  const providerFile = join(directory, "user's providers.json");
  const valid = spawnSync(process.execPath, args, { encoding: 'utf8', env: {
    HOME: directory, PATH: '/usr/bin:/bin', XDG_STATE_HOME: join(directory, 'state'),
    OPENCODE_CONFIG_DIR: opencode, OMC_WRAPPER_PROVIDER_CONFIG: providerFile,
  } });
  assert.equal(valid.status, 0, valid.stderr);
  const content = readFileSync(join(cfg, 'service.env'), 'utf8');
  assert.ok(content.includes(`OPENCODE_CONFIG_DIR="${opencode}"`));
  assert.ok(content.includes(`OMC_WRAPPER_PROVIDER_CONFIG="${providerFile}"`));
});
