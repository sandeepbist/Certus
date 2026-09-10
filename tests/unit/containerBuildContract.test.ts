import { describe, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const repositoryRoot = path.resolve(import.meta.dir, '../..');
const readRepositoryFile = (relativePath: string) =>
  readFileSync(path.join(repositoryRoot, relativePath), 'utf8');

describe('container build contracts', () => {
  test('the root context sends only locked manifests and service sources', () => {
    const dockerIgnore = readRepositoryFile('.dockerignore');

    expect(dockerIgnore.split(/\r?\n/).filter(Boolean)).toEqual([
      '**',
      '!package.json',
      '!bun.lock',
      '!bunfig.toml',
      '!services/',
      '!services/**',
    ]);
  });

  test('javascript images fail closed on the committed Bun workspace lock', () => {
    for (const service of ['gateway', 'web']) {
      const dockerfile = readRepositoryFile(`services/${service}/Dockerfile`);

      expect(dockerfile).toContain('oven/bun:1.4.0-alpine');
      expect(dockerfile).toContain('COPY package.json bun.lock bunfig.toml ./');
      expect(dockerfile).toContain('bun install --frozen-lockfile');
      expect(dockerfile).not.toContain('||');
      expect(dockerfile).toContain('USER certus');
    }
  });

  test('the web image runs the traced standalone server', () => {
    const config = readRepositoryFile('services/web/next.config.mjs');
    const dockerfile = readRepositoryFile('services/web/Dockerfile');

    expect(config).toContain("output: 'standalone'");
    expect(dockerfile).toContain('/app/services/web/.next/standalone');
    expect(dockerfile).toContain('CMD ["bun", "server.js"]');
    expect(dockerfile).not.toContain('COPY --from=builder /app/public');
  });

  test('request-bound authentication stays out of static generation', () => {
    const database = readRepositoryFile('services/web/src/lib/db.ts');
    const auth = readRepositoryFile('services/web/src/lib/auth.ts');
    const onboarding = readRepositoryFile('services/web/src/app/(auth)/onboarding/page.tsx');

    expect(database).toContain('export function getDb()');
    expect(auth).toContain('export function getAuth()');
    expect(onboarding).toContain("export const dynamic = 'force-dynamic'");
    expect(auth).not.toContain('export const auth = betterAuth');
  });

  test('web database startup and shutdown run only in the Node server runtime', () => {
    const instrumentation = readRepositoryFile('services/web/src/instrumentation.ts');
    const nodeInstrumentation = readRepositoryFile('services/web/src/instrumentation-node.ts');
    const database = readRepositoryFile('services/web/src/lib/db.ts');

    expect(instrumentation).toContain("process.env.NEXT_RUNTIME === 'nodejs'");
    expect(instrumentation).toContain("import('./instrumentation-node')");
    expect(instrumentation).toContain('process.exit(1)');
    expect(nodeInstrumentation).toContain('assertWebDatabaseConfiguration()');
    expect(nodeInstrumentation).toContain("process.once('SIGTERM', closeDatabase)");
    expect(database).toContain('export async function closeDbPool()');
    expect(database).toContain("application_name: 'certus-web'");
    expect(database).toContain('idle_in_transaction_session_timeout:');
  });

  test('gateway loads local configuration before route modules', () => {
    const server = readRepositoryFile('services/gateway/src/server.ts');
    const environmentImport = "import './config/environment.js';";
    expect(server.indexOf(environmentImport)).toBeGreaterThanOrEqual(0);
    expect(server.indexOf(environmentImport)).toBeLessThan(server.indexOf("import fastify from 'fastify';"));
    expect(server).not.toContain('dotenv.config(');
  });

  test('web readiness validates runtime auth and database state without leaking errors', () => {
    const readiness = readRepositoryFile('services/web/src/app/api/health/ready/route.ts');

    expect(readiness).toContain("export const dynamic = 'force-dynamic'");
    expect(readiness).toContain("getDb().query('SELECT 1 AS ready')");
    expect(readiness).toContain('{ status: 503');
    expect(readiness).not.toContain('error.message');
  });

  test('the MCP image uses repository-root source paths', () => {
    const dockerfile = readRepositoryFile('services/mcp-tools/Dockerfile');

    expect(dockerfile).toContain('services/mcp-tools/requirements.txt');
    expect(dockerfile).toContain('services/mcp-tools/app/');
  });

  test('the embedding image excludes compilers and constrains dependency majors', () => {
    const dockerfile = readRepositoryFile('services/embedding/Dockerfile');
    const requirements = readRepositoryFile('services/embedding/requirements.txt');

    expect(dockerfile).toContain('--only-binary=:all:');
    expect(dockerfile).not.toContain('build-essential');
    expect(dockerfile).not.toContain('apt-get');
    for (const upperBound of ['openai>=3.3.1,<4', 'redis>=5.2.1,<6', 'pydantic>=2.10.0,<3']) {
      expect(requirements).toContain(upperBound);
    }
  });

  test('the ingestion image uses wheels only and excludes an unused compiler toolchain', () => {
    const dockerfile = readRepositoryFile('services/ingestion/Dockerfile');
    const requirements = readRepositoryFile('services/ingestion/requirements.txt');

    expect(dockerfile).toContain('--only-binary=:all:');
    expect(dockerfile).not.toContain('build-essential');
    expect(dockerfile).not.toContain('apt-get');
    for (const upperBound of ['fastapi>=0.141.1,<1', 'neo4j>=6.3.0,<7', 'pydantic>=2.13.4,<3']) {
      expect(requirements).toContain(upperBound);
    }
  });

  test('every application image runs as a dedicated non-root user', () => {
    for (const service of [
      'embedding',
      'gateway',
      'ingestion',
      'mcp-tools',
      'orchestration',
      'web',
      'workflows',
    ]) {
      const dockerfile = readRepositoryFile(`services/${service}/Dockerfile`);

      expect(dockerfile).toContain('USER certus');
      if (!['gateway', 'web'].includes(service)) {
        expect(dockerfile).toContain('chmod -R a=rX /app');
      }
    }
  });
});
