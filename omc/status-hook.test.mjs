import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { updatesForHook } from './status-hook.mjs';

test('orchestrator receives changed worker block/model status once per observation', t => {
  const state = mkdtempSync(join(tmpdir(), 'omc-status-hook-'));
  t.after(() => rmSync(state, { recursive: true, force: true }));
  const runId = randomUUID(), jobId = randomUUID();
  const dir = join(state, 'runs', runId, 'jobs');
  mkdirSync(dir, { recursive: true });
  const job = { kind: 'agent', run_id: runId, job_id: jobId, provider: 'codex', worker: 'team/worker-1',
    requested_model: 'gpt-6-sol', job_state: 'running', provider_status: { outcome: 'blocked',
      actual_model: null, block: { detected: true, category: 'cybersecurity', source: 'provider_event' } } };
  const file = join(dir, jobId + '.json');
  writeFileSync(file, JSON.stringify(job));
  const env = { OMC_WRAPPER_RUN_ID: runId, OMC_WRAPPER_STATE_DIR: state };
  const input = { session_id: 'fixture-session', hook_event_name: 'PostToolUse' };
  const first = updatesForHook(input, env);
  assert.match(first.hookSpecificOutput.additionalContext, /cybersecurity/);
  assert.match(first.hookSpecificOutput.additionalContext, /team\/worker-1/);
  assert.equal(updatesForHook(input, env), null);
  job.provider_status = { outcome: 'completed', actual_model: 'gpt-fallback-observed', model_switches: [{ from: 'gpt-6-sol', to: 'gpt-fallback-observed' }] };
  writeFileSync(file, JSON.stringify(job));
  assert.match(updatesForHook(input, env).hookSpecificOutput.additionalContext, /gpt-fallback-observed/);
  assert.equal(updatesForHook(input, { ...env, OMC_TEAM_WORKER: 'team/worker-2' }), null);
  assert.equal(updatesForHook({ ...input, agent_id: 'subagent' }, env), null);
  assert.equal(updatesForHook(input, {}), null);
});
