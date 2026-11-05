import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { nativeBinaries, configRoot, installation } from './installation.mjs';

test('native binary discovery skips wrapper shims, follows real binaries and honors explicit roots', t => {
  const root = mkdtempSync(join(tmpdir(), 'omc-installation-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const first = join(root, 'first'), second = join(root, 'second'), cfg = join(root, 'config');
  for (const dir of [first, second, cfg]) mkdirSync(dir);
  const shim = join(root, 'provider-cli.sh');
  writeFileSync(shim, '#!/bin/sh\nexit 9\n', { mode: 0o700 });
  for (const provider of ['claude', 'codex']) {
    symlinkSync(shim, join(first, provider));
    writeFileSync(join(second, provider), '#!/bin/sh\nexit 0\n', { mode: 0o700 });
  }
  const expected = { claude: join(second, 'claude'), codex: join(second, 'codex') };
  const env = { PATH: `${first}:${second}`, OMC_WRAPPER_CONFIG_DIR: cfg };
  assert.deepEqual(nativeBinaries(env), expected);
  writeFileSync(join(cfg, 'install.json'), JSON.stringify({ native_binaries: expected }));
  assert.deepEqual(nativeBinaries({ ...env, PATH: first }), expected);
  assert.equal(installation(env).native_binaries.codex, expected.codex);
  assert.equal(configRoot({ XDG_CONFIG_HOME: root }), join(root, 'omc-provider-wrapper'));
  assert.throws(() => nativeBinaries({ OMC_WRAPPER_CONFIG_DIR: join(root, 'absent'), PATH: first }), /Native claude CLI missing/);
});
