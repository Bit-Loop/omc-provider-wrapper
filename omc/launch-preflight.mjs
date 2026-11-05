import { spawnSync } from 'node:child_process';
import { join } from 'node:path';

export function isNativeLaunch(args) {
  const first = args[0];
  return !first || first === 'launch'
    || (first.startsWith('-') && !['--help', '-h', '--version', '-V'].includes(first));
}

export function checkWrappedClaude(profile, cwd = process.cwd()) {
  const executable = join(profile.run.provider_path, 'claude');
  const check = spawnSync(executable, ['--version'], {
    cwd, env: profile.env, encoding: 'utf8', timeout: 10000, maxBuffer: 65536,
  });
  if (check.error || check.status !== 0) {
    let detail = (check.stderr || check.error?.message || `exit ${check.status}`).trim();
    for (const [key, value] of Object.entries(profile.env)) {
      if (value && value.length >= 6 && /key|token|password|secret|credential/i.test(key)) {
        detail = detail.split(value).join('[redacted]');
      }
    }
    throw new Error(`Wrapped Claude startup check failed: ${detail.slice(0, 1800)}`);
  }
  return check.stdout.trim();
}
