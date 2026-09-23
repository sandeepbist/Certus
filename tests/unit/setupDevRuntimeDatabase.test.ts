import { afterEach, describe, expect, test } from 'bun:test';
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const repositoryRoot = path.resolve(import.meta.dir, '../..');
const temporaryDirectories: string[] = [];
const oldLocalUrl = 'postgresql://nexus:nexus_dev_password@localhost:5432/nexus';
const localSecrets = [
  `BETTER_AUTH_SECRET=${'a'.repeat(40)}`,
  `INTERNAL_SERVICE_TOKEN=${'b'.repeat(40)}`,
  `OBJECT_STORAGE_ACCESS_KEY=${'c'.repeat(40)}`,
  `OBJECT_STORAGE_SECRET_KEY=${'d'.repeat(40)}`,
].join('\n');

function temporaryDirectory() {
  const directory = mkdtempSync(path.join(tmpdir(), 'certus-runtime-db-'));
  temporaryDirectories.push(directory);
  return directory;
}

function writeExecutable(filePath: string, contents: string) {
  writeFileSync(filePath, contents);
  chmodSync(filePath, 0o700);
}

function createSetupFixture(envContents: string) {
  const root = temporaryDirectory();
  const bin = path.join(root, 'test-bin');
  const scripts = path.join(root, 'scripts');
  const library = path.join(scripts, 'lib');
  const virtualEnv = path.join(root, '.venv', 'bin');
  mkdirSync(bin, { recursive: true });
  mkdirSync(library, { recursive: true });
  mkdirSync(virtualEnv, { recursive: true });
  copyFileSync(path.join(repositoryRoot, 'scripts/setup-dev.sh'), path.join(scripts, 'setup-dev.sh'));
  copyFileSync(path.join(repositoryRoot, 'scripts/lib/env-file.sh'), path.join(library, 'env-file.sh'));
  copyFileSync(path.join(repositoryRoot, '.env.example'), path.join(root, '.env.example'));
  writeFileSync(path.join(root, '.env'), `${envContents.trim()}\n`, { mode: 0o600 });

  writeExecutable(path.join(bin, 'bun'), '#!/bin/bash\nexit 0\n');
  writeExecutable(
    path.join(bin, 'certus-python-check'),
    '#!/bin/bash\nexit 0\n',
  );
  writeExecutable(
    path.join(bin, 'bash'),
    '#!/bin/bash\nif [[ "$1" == "-c" && "$2" == "</dev/tcp/127.0.0.1/"* ]]; then exit 0; fi\nexec /bin/bash "$@"\n',
  );
  writeExecutable(
    path.join(bin, 'docker'),
    [
      '#!/bin/bash',
      'if [[ "$1" == "exec" ]]; then exit 0; fi',
      'if [[ "$1" == "compose" && "$2" == "up" ]]; then',
      '  if [[ " $* " == *" workflows "* ]]; then',
      '    printf "workflows|%s\\n" "$DATABASE_URL" >> "$CERTUS_SETUP_TEST_LOG"',
      '  else',
      '    printf "infrastructure\\n" >> "$CERTUS_SETUP_TEST_LOG"',
      '  fi',
      '  exit 0',
      'fi',
      'exit 0',
      '',
    ].join('\n'),
  );
  writeExecutable(path.join(virtualEnv, 'python'), '#!/bin/bash\nexit 0\n');
  writeExecutable(
    path.join(scripts, 'migrate-db.sh'),
    '#!/bin/bash\nprintf "database-migration|%s|%s|%s\\n" "$DATABASE_URL" "$CERTUS_RUNTIME_DB_USER" "$CERTUS_RUNTIME_DB_PASSWORD" >> "$CERTUS_SETUP_TEST_LOG"\n',
  );
  writeExecutable(
    path.join(scripts, 'migrate-neo4j.sh'),
    '#!/bin/bash\nprintf "graph-migration\\n" >> "$CERTUS_SETUP_TEST_LOG"\n',
  );

  const eventLog = path.join(root, 'setup-events.log');
  const runSetup = () =>
    Bun.spawnSync({
      cmd: ['/bin/bash', 'scripts/setup-dev.sh'],
      cwd: root,
      env: {
        PATH: `${bin}:${process.env.PATH || '/usr/bin:/bin'}`,
        CERTUS_SETUP_TEST_LOG: eventLog,
        PYTHON_COMMAND: 'certus-python-check',
        LC_ALL: 'C',
      },
      stdout: 'pipe',
      stderr: 'pipe',
    });

  return { root, eventLog, runSetup };
}

function envValue(envContents: string, name: string) {
  const assignments = [...envContents.matchAll(new RegExp(`^${name}=(.*)$`, 'gm'))];
  return assignments.at(-1)?.[1] ?? '';
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('local runtime database setup', () => {
  test('converts the exact old local URL once and keeps generated credentials stable', () => {
    const fixture = createSetupFixture(
      [
        'DB_HOST=localhost',
        'DB_PORT=5432',
        'POSTGRES_USER=nexus',
        'POSTGRES_PASSWORD=nexus_dev_password',
        'POSTGRES_DB=nexus',
        'CERTUS_RUNTIME_DB_USER=certus_runtime',
        `DATABASE_URL=${oldLocalUrl}`,
        localSecrets,
      ].join('\n'),
    );

    const first = fixture.runSetup();
    expect(first.exitCode).toBe(0);
    expect(first.stdout.toString()).toContain('Converting the exact legacy local DATABASE_URL');

    const envPath = path.join(fixture.root, '.env');
    const firstEnv = readFileSync(envPath, 'utf8');
    const runtimePassword = envValue(firstEnv, 'CERTUS_RUNTIME_DB_PASSWORD');
    const runtimeUrl = envValue(firstEnv, 'DATABASE_URL');
    const parsedUrl = new URL(runtimeUrl);
    expect(runtimePassword).toMatch(/^[a-f0-9]{64}$/);
    expect(parsedUrl.username).toBe('certus_runtime');
    expect(decodeURIComponent(parsedUrl.password)).toBe(runtimePassword);
    expect(parsedUrl.hostname).toBe('localhost');
    expect(parsedUrl.port).toBe('5432');
    expect(parsedUrl.pathname).toBe('/nexus');
    expect(runtimeUrl).not.toBe(oldLocalUrl);

    const second = fixture.runSetup();
    expect(second.exitCode).toBe(0);
    expect(second.stdout.toString()).not.toContain('Converting the exact legacy local DATABASE_URL');
    const secondEnv = readFileSync(envPath, 'utf8');
    expect(envValue(secondEnv, 'CERTUS_RUNTIME_DB_PASSWORD')).toBe(runtimePassword);
    expect(envValue(secondEnv, 'DATABASE_URL')).toBe(runtimeUrl);

    const events = readFileSync(fixture.eventLog, 'utf8').trim().split('\n');
    const migrations = events.filter((event) => event.startsWith('database-migration|'));
    const workflowStarts = events.filter((event) => event.startsWith('workflows|'));
    const migrationPositions = events.flatMap((event, index) =>
      event.startsWith('database-migration|') ? [index] : [],
    );
    const workflowPositions = events.flatMap((event, index) =>
      event.startsWith('workflows|') ? [index] : [],
    );
    expect(migrations).toHaveLength(2);
    expect(workflowStarts).toHaveLength(2);
    for (let index = 0; index < 2; index += 1) {
      expect(migrationPositions[index]).toBeLessThan(workflowPositions[index]);
      const [, migrationUrl, migrationUser, migrationPassword] = migrations[index].split('|');
      expect(migrationUrl).toBe(runtimeUrl);
      expect(migrationUser).toBe('certus_runtime');
      expect(migrationPassword).toBe(runtimePassword);
      expect(workflowStarts[index]).toBe(`workflows|${runtimeUrl}`);
    }
  });

  test('fails on a conflicting custom URL without rewriting database credentials', () => {
    const originalEnv = [
      'DB_HOST=localhost',
      'DB_PORT=5432',
      'POSTGRES_USER=certus_migrator',
      'POSTGRES_PASSWORD=migration-secret-custom',
      'POSTGRES_DB=certus_prod',
      'CERTUS_RUNTIME_DB_USER=certus_app',
      'CERTUS_RUNTIME_DB_PASSWORD=runtime-secret-custom',
      'DATABASE_URL=postgresql://wrong:wrong@localhost:5432/certus_prod',
      localSecrets,
    ].join('\n');
    const fixture = createSetupFixture(originalEnv);

    const result = fixture.runSetup();
    const output = `${result.stdout.toString()}${result.stderr.toString()}`;
    expect(result.exitCode).not.toBe(0);
    expect(output).toContain('DATABASE_URL conflicts with the runtime role or database target');
    expect(output).toContain('scripts/migrate-db.sh');
    expect(readFileSync(path.join(fixture.root, '.env'), 'utf8')).toBe(`${originalEnv}\n`);
    expect(existsSync(fixture.eventLog)).toBe(false);
  });

  test('does not retarget the legacy URL after database settings change', () => {
    const originalEnv = [
      'DB_HOST=localhost',
      'DB_PORT=5432',
      'POSTGRES_USER=nexus',
      'POSTGRES_PASSWORD=nexus_dev_password',
      'POSTGRES_DB=certus_prod',
      `DATABASE_URL=${oldLocalUrl}`,
      localSecrets,
    ].join('\n');
    const fixture = createSetupFixture(originalEnv);

    const result = fixture.runSetup();
    const output = `${result.stdout.toString()}${result.stderr.toString()}`;
    expect(result.exitCode).not.toBe(0);
    expect(output).toContain('legacy local DATABASE_URL can be converted only with the original local database target');
    expect(readFileSync(path.join(fixture.root, '.env'), 'utf8')).toBe(`${originalEnv}\n`);
    expect(existsSync(fixture.eventLog)).toBe(false);
  });

  test('rejects managed hosts and preserves their credentials', () => {
    const originalEnv = [
      'DB_HOST=managed.example',
      'DB_PORT=5432',
      'POSTGRES_USER=certus_migrator',
      'POSTGRES_PASSWORD=migration-secret-custom',
      'POSTGRES_DB=certus_prod',
      'CERTUS_RUNTIME_DB_USER=certus_app',
      'CERTUS_RUNTIME_DB_PASSWORD=runtime:secret',
      'DATABASE_URL=postgresql://certus_app:runtime%3Asecret@managed.example:5432/certus_prod',
      localSecrets,
    ].join('\n');
    const fixture = createSetupFixture(originalEnv);

    const result = fixture.runSetup();
    const output = `${result.stdout.toString()}${result.stderr.toString()}`;
    expect(result.exitCode).not.toBe(0);
    expect(output).toContain('make setup only supports local PostgreSQL hosts');
    expect(output).toContain('scripts/migrate-db.sh');
    expect(readFileSync(path.join(fixture.root, '.env'), 'utf8')).toBe(`${originalEnv}\n`);
    expect(existsSync(fixture.eventLog)).toBe(false);
  });

  test('preserves coherent custom migration and runtime credentials for a local target', () => {
    const originalEnv = [
      'DB_HOST=localhost',
      'DB_PORT=5432',
      'POSTGRES_USER=certus_migrator',
      'POSTGRES_PASSWORD=migration-secret-custom',
      'POSTGRES_DB=certus_local',
      'CERTUS_RUNTIME_DB_USER=certus_app',
      'CERTUS_RUNTIME_DB_PASSWORD=runtime:secret',
      'DATABASE_URL=postgresql://certus_app:runtime%3Asecret@localhost:5432/certus_local',
      localSecrets,
    ].join('\n');
    const fixture = createSetupFixture(originalEnv);

    const result = fixture.runSetup();
    expect(result.exitCode).toBe(0);
    expect(result.stdout.toString()).not.toContain('Converting the exact legacy local DATABASE_URL');
    expect(readFileSync(path.join(fixture.root, '.env'), 'utf8')).toBe(`${originalEnv}\n`);
    const events = readFileSync(fixture.eventLog, 'utf8').split('\n');
    expect(events).toContain(
      'database-migration|postgresql://certus_app:runtime%3Asecret@localhost:5432/certus_local|certus_app|runtime:secret',
    );
    expect(events).toContain(
      'workflows|postgresql://certus_app:runtime%3Asecret@localhost:5432/certus_local',
    );
  });
});

describe('Compose runtime database interpolation', () => {
  test('requires runtime DATABASE_URL and waits for the provisioned role', () => {
    const compose = readFileSync(path.join(repositoryRoot, 'docker-compose.yml'), 'utf8');
    expect(compose).toContain(
      'DATABASE_URL: ${DATABASE_URL:?DATABASE_URL must use CERTUS_RUNTIME_DB_USER and CERTUS_RUNTIME_DB_PASSWORD}',
    );
    expect(compose).toContain('POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required; run make setup for local development}');
    expect(compose).toContain('condition: service_healthy');
    expect(compose).toContain('FROM pg_roles AS role');
    expect(compose).toContain("has_database_privilege(role.oid, current_database(), 'CONNECT')");
    expect(compose).toContain("has_schema_privilege(role.oid, 'public', 'USAGE')");
    expect(compose).toContain('PGREQUIREAUTH=password,md5,scram-sha-256');
    expect(compose).toContain('PGPASSWORD="$${CERTUS_RUNTIME_DB_PASSWORD}"');
    expect(compose).not.toContain(oldLocalUrl);

    if (!Bun.which('docker')) return;

    const runtimeUrl = 'postgresql://certus_runtime:runtime-secret@localhost:5432/nexus';
    const config = Bun.spawnSync({
      cmd: ['docker', 'compose', '-f', path.join(repositoryRoot, 'docker-compose.yml'), 'config', '--format', 'json'],
      cwd: repositoryRoot,
      env: {
        PATH: process.env.PATH || '/usr/bin:/bin',
        DATABASE_URL: runtimeUrl,
        POSTGRES_PASSWORD: 'migration-secret',
        CERTUS_RUNTIME_DB_USER: 'certus_runtime',
        CERTUS_RUNTIME_DB_PASSWORD: 'runtime-secret',
      },
      stdout: 'pipe',
      stderr: 'pipe',
    });
    expect(config.exitCode).toBe(0);
    const resolved = JSON.parse(config.stdout.toString());
    expect(resolved.services.workflows.environment.DATABASE_URL).toBe(runtimeUrl);
    expect(resolved.services.workflows.depends_on.postgres.condition).toBe('service_healthy');
    const postgresHealthcheck = resolved.services.postgres.healthcheck.test.join(' ');
    expect(postgresHealthcheck).toContain('FROM pg_roles AS role');
    expect(postgresHealthcheck).toContain('has_database_privilege');
    expect(postgresHealthcheck).toContain('has_schema_privilege');
    expect(postgresHealthcheck).toContain('PGREQUIREAUTH=password,md5,scram-sha-256');
    expect(postgresHealthcheck).toContain('PGPASSWORD="$${CERTUS_RUNTIME_DB_PASSWORD}"');

    const missingUrl = Bun.spawnSync({
      cmd: ['docker', 'compose', '-f', path.join(repositoryRoot, 'docker-compose.yml'), 'config', '--format', 'json'],
      cwd: repositoryRoot,
      env: {
        PATH: process.env.PATH || '/usr/bin:/bin',
        DATABASE_URL: '',
        POSTGRES_PASSWORD: 'migration-secret',
        CERTUS_RUNTIME_DB_USER: 'certus_runtime',
        CERTUS_RUNTIME_DB_PASSWORD: 'runtime-secret',
      },
      stdout: 'pipe',
      stderr: 'pipe',
    });
    expect(missingUrl.exitCode).not.toBe(0);
    expect(missingUrl.stderr.toString()).toContain('DATABASE_URL must use CERTUS_RUNTIME_DB_USER');

    const missingRuntimePassword = Bun.spawnSync({
      cmd: ['docker', 'compose', '-f', path.join(repositoryRoot, 'docker-compose.yml'), 'config', '--format', 'json'],
      cwd: repositoryRoot,
      env: {
        PATH: process.env.PATH || '/usr/bin:/bin',
        DATABASE_URL: runtimeUrl,
        POSTGRES_PASSWORD: 'migration-secret',
        CERTUS_RUNTIME_DB_USER: 'certus_runtime',
        CERTUS_RUNTIME_DB_PASSWORD: '',
      },
      stdout: 'pipe',
      stderr: 'pipe',
    });
    expect(missingRuntimePassword.exitCode).not.toBe(0);
    expect(missingRuntimePassword.stderr.toString()).toContain('CERTUS_RUNTIME_DB_PASSWORD is required');
  }, 15_000);
});
