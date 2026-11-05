#!/usr/bin/env node
import { readFileSync, readdirSync, mkdirSync, writeFileSync, renameSync, statSync } from 'node:fs';
import { join, isAbsolute } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

export function updatesForHook(input, env = process.env) {
  const runId = env.OMC_WRAPPER_RUN_ID;
  const state = env.OMC_WRAPPER_STATE_DIR;
  if (!UUID.test(runId || '') || !state || !isAbsolute(state) || env.OMC_TEAM_WORKER || input.agent_id) return null;
  const event = input.hook_event_name;
  if (!['PostToolUse', 'UserPromptSubmit'].includes(event)) return null;
  const session = String(input.session_id || env.OMC_WRAPPER_JOB_ID || 'main');
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(session)) return null;
  const root = join(state, 'runs', runId);
  const jobsDir = join(root, 'jobs');
  const cursorDir = join(root, 'hook-cursors');
  const cursorFile = join(cursorDir, `${session}.json`);
  let cursor = {};
  try { cursor = JSON.parse(readFileSync(cursorFile, 'utf8')); } catch {}
  let names;
  try { names = readdirSync(jobsDir).filter(n => UUID.test(n.replace(/\.json$/, '')) && n.endsWith('.json'))
    .map(name => ({ name, modified: statSync(join(jobsDir, name)).mtimeMs }))
    .sort((a, b) => b.modified - a.modified).slice(0, 200).map(item => item.name); }
  catch { return null; }
  const updates = [];
  const next = {};
  for (const name of names) {
    try {
      const job = JSON.parse(readFileSync(join(jobsDir, name), 'utf8'));
      if (job.run_id !== runId || job.kind !== 'agent') continue;
      const report = { job_id: job.job_id, provider: job.provider, worker: job.worker || null,
        role: job.role || null, job_state: job.job_state, exit_code: job.exit_code,
        requested_model: job.requested_model ?? null, provider_status: job.provider_status,
        subagents: job.subagents || [] };
      const hash = createHash('sha256').update(JSON.stringify(report)).digest('hex');
      next[name] = hash;
      if (cursor[name] !== hash) updates.push(report);
    } catch { /* A replaced/in-progress job snapshot will be read on the next hook. */ }
  }
  if (!updates.length) return null;
  mkdirSync(cursorDir, { recursive: true, mode: 0o700 });
  const temporary = cursorFile + '.' + randomUUID();
  writeFileSync(temporary, JSON.stringify(next), { mode: 0o600, flag: 'wx' });
  renameSync(temporary, cursorFile);
  return { hookSpecificOutput: { hookEventName: event,
    additionalContext: 'Provider wrapper updates for this OMC run. These are observations from provider output; a null actual_model means unknown. ' + JSON.stringify({ run_id: runId, jobs: updates }) } };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    let body = '';
    for await (const chunk of process.stdin) {
      body += chunk;
      if (Buffer.byteLength(body) > 1_048_576) throw new Error('Hook input too large.');
    }
    const output = updatesForHook(JSON.parse(body || '{}'));
    if (output) process.stdout.write(JSON.stringify(output) + '\n');
  } catch { /* Observability hooks must not interrupt the user's native session. */ }
}
