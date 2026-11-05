#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { installation } from '../omc/installation.mjs';
import { readServiceEnv } from '../omc/advisor.mjs';
import { fileURLToPath } from 'node:url';

const config = installation();
if (!config.service_env) throw new Error('Run install.sh first.');
const env = { ...process.env, ...readServiceEnv(config.service_env) };
delete env.OPENAI_API_KEY;
delete env.ANTHROPIC_API_KEY;
const child = spawn(config.python || 'python3', ['-B', fileURLToPath(new URL('../agent_chat_api/app.py', import.meta.url)), ...process.argv.slice(2)],
  { stdio: 'inherit', env });
for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => child.kill(signal));
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 128 + ({ SIGINT: 2, SIGTERM: 15, SIGHUP: 1 }[signal] || 1) : 1); });
