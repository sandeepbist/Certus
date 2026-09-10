import { afterEach, describe, expect, test } from 'bun:test';
import Fastify from 'fastify';

import { embeddingGenerationRoutes } from '../../services/gateway/src/routes/embeddingGenerations';


const originalFetch = globalThis.fetch;
const originalInternalToken = process.env.INTERNAL_SERVICE_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalInternalToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
  else process.env.INTERNAL_SERVICE_TOKEN = originalInternalToken;
});

describe('embedding generation operator boundary', () => {
  test('allows scoped reads but restricts mutations to trusted operators', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    const requests: Array<{ url: string; method: string; headers: Headers }> = [];
    globalThis.fetch = (async (input, init = {}) => {
      requests.push({
        url: String(input),
        method: init.method || 'GET',
        headers: new Headers(init.headers),
      });
      return Response.json({ ok: true }, { status: 200 });
    }) as typeof fetch;

    const app = Fastify({ logger: false });
    app.addHook('preHandler', async (request) => {
      const role = String(request.headers['x-test-role'] || 'member');
      const authType = request.headers['x-test-auth'] === 'api'
        ? 'api_key'
        : 'session';
      request.user = {
        userId: 'user-proof',
        tenantId: 'tenant-proof',
        roles: [role],
        tokenBudget: 1_000,
        authType,
        scopes: request.headers['x-test-admin'] === 'true'
          ? ['admin']
          : ['write'],
      };
    });
    await app.register(embeddingGenerationRoutes);

    try {
      expect((await app.inject({
        method: 'GET',
        url: '/api/embedding-generations?limit=25&status=building',
      })).statusCode).toBe(200);

      expect((await app.inject({
        method: 'POST',
        url: '/api/embedding-generations',
        payload: { embedding_profile: 'profile' },
      })).statusCode).toBe(403);

      expect((await app.inject({
        method: 'POST',
        url: '/api/embedding-generations',
        headers: { 'x-test-role': 'owner' },
        payload: { embedding_profile: 'profile' },
      })).statusCode).toBe(200);

      expect((await app.inject({
        method: 'POST',
        url: '/api/embedding-generations/id/cancel',
        headers: { 'x-test-role': 'owner', 'x-test-auth': 'api' },
      })).statusCode).toBe(403);

      expect((await app.inject({
        method: 'POST',
        url: '/api/embedding-generations/id/rollback',
        headers: {
          'x-test-role': 'admin',
          'x-test-auth': 'api',
          'x-test-admin': 'true',
        },
      })).statusCode).toBe(200);
    } finally {
      await app.close();
    }

    expect(requests.map(({ url, method }) => ({ url, method }))).toEqual([
      {
        url: 'http://localhost:8002/embedding-generations?limit=25&status=building',
        method: 'GET',
      },
      {
        url: 'http://localhost:8002/embedding-generations',
        method: 'POST',
      },
      {
        url: 'http://localhost:8002/embedding-generations/id/rollback',
        method: 'POST',
      },
    ]);
    for (const request of requests) {
      expect(request.headers.get('x-certus-tenant-id')).toBe('tenant-proof');
      expect(request.headers.get('x-certus-user-id')).toBe('user-proof');
      expect(request.headers.get('x-internal-service-token')).toBe(
        'test-internal-service-token-32-characters',
      );
    }
  });
});
