import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { checkWrappedClaude, isNativeLaunch } from './launch-preflight.mjs';

test('native launches are checked without checking goal registration or inspection commands', () => {
  for (const args of [[], ['launch', '--yolo'], ['--yolo'], ['-p', 'prompt']]) assert.equal(isNativeLaunch(args), true);
  for (const args of [['ultragoal', 'create-goals'], ['models', 'show'], ['wrapper', 'status'], ['--version'], ['--help']]) assert.equal(isNativeLaunch(args), false);
});

test('startup check exercises the wrapped executable and exposes a broken shim cause', t => {
  const directory = mkdtempSync(join(tmpdir(), 'omc-preflight-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const filename = join(directory, 'claude');
  const profile = { run: { provider_path: directory }, env: { PATH: '/usr/bin', TEST_API_KEY: 'fixture-secret' } };
  writeFileSync(filename, '#!/bin/sh\ntest "$1" = --version || exit 99\nprintf "fixture-claude 1.0\\n"\n', { mode: 0o700 });
  assert.equal(checkWrappedClaude(profile), 'fixture-claude 1.0');
  writeFileSync(filename, '#!/bin/sh\nprintf "native wrapper entrypoint unavailable fixture-secret\\n" >&2\nexit 2\n');
  assert.throws(() => checkWrappedClaude(profile), error => {
    assert.match(error.message, /Wrapped Claude startup check failed: native wrapper entrypoint unavailable/);
    assert.doesNotMatch(error.message, /fixture-secret/);
    return true;
  });
  rmSync(filename);
  assert.throws(() => checkWrappedClaude(profile), /Wrapped Claude startup check failed/);
});
