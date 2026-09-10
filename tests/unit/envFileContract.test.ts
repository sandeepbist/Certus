import { afterEach, describe, expect, test } from 'bun:test';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const repositoryRoot = path.resolve(import.meta.dir, '../..');
const envLibrary = path.join(repositoryRoot, 'scripts/lib/env-file.sh');
const temporaryDirectories: string[] = [];

function temporaryDirectory() {
  const directory = mkdtempSync(path.join(tmpdir(), 'certus-env-contract-'));
  temporaryDirectories.push(directory);
  return directory;
}

function runBash(script: string, args: string[] = [], env: Record<string, string> = {}) {
  const result = Bun.spawnSync({
    cmd: ['bash', '-c', script, 'certus-env-contract', ...args],
    env: { PATH: process.env.PATH || '/usr/bin:/bin', ...env },
    stdout: 'pipe',
    stderr: 'pipe',
  });
  if (result.exitCode !== 0) {
    throw new Error(result.stderr.toString());
  }
  return result.stdout.toString();
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('literal env-file contracts', () => {
  test('reads quotes and last assignments without evaluating shell syntax', () => {
    const directory = temporaryDirectory();
    const envFile = path.join(directory, '.env');
    const marker = path.join(directory, 'executed');
    writeFileSync(
      envFile,
      [
        'QUOTED_VALUE="first value"',
        'QUOTED_VALUE=\'last value\'',
        `UNSAFE_VALUE=$(touch ${marker})`,
        '',
      ].join('\n'),
      { mode: 0o600 },
    );

    const quoted = runBash(
      'source "$1"; certus_read_env_value "$2" QUOTED_VALUE',
      [envLibrary, envFile],
    );
    const unsafe = runBash(
      'source "$1"; certus_read_env_value "$2" UNSAFE_VALUE',
      [envLibrary, envFile],
    );

    expect(quoted).toBe('last value');
    expect(unsafe).toBe(`$(touch ${marker})`);
    expect(existsSync(marker)).toBe(false);
  });

  test('preserves an explicit process value before file and fallback values', () => {
    const directory = temporaryDirectory();
    const envFile = path.join(directory, '.env');
    writeFileSync(envFile, 'SERVICE_VALUE=from-file\n', { mode: 0o600 });

    const value = runBash(
      'source "$1"; certus_export_env_default SERVICE_VALUE fallback "$2"; printf %s "$SERVICE_VALUE"',
      [envLibrary, envFile],
      { SERVICE_VALUE: 'from-process' },
    );

    expect(value).toBe('from-process');
  });

  test('migration scripts never execute the dotenv file', () => {
    for (const script of ['scripts/migrate-db.sh', 'scripts/migrate-neo4j.sh']) {
      const source = readFileSync(path.join(repositoryRoot, script), 'utf8');

      expect(source).toContain('scripts/lib/env-file.sh');
      expect(source).not.toMatch(/source\s+["']?\.env/);
    }

    const setup = readFileSync(path.join(repositoryRoot, 'scripts/setup-dev.sh'), 'utf8');
    expect(setup).toContain('chmod 600 .env');
  });

  test('PostgreSQL migrations use one session-level release lock', () => {
    const source = readFileSync(path.join(repositoryRoot, 'scripts/migrate-db.sh'), 'utf8');

    expect(source).toContain("pg_advisory_lock(hashtextextended('certus:schema-migrations:v1', 0))");
    expect(source).toContain("pg_advisory_unlock(hashtextextended('certus:schema-migrations:v1', 0))");
    expect(source.match(/\| run_psql -q/g)).toHaveLength(1);
    expect(source).toContain('MIGRATION_LOCK_TIMEOUT_SECONDS');
    expect(source).toContain('Applied migration was modified:');
  });
});
