import { afterEach, describe, expect, test } from 'bun:test';
import Fastify from 'fastify';

import { documentRoutes } from '../../services/gateway/src/routes/documents';

const originalFetch = globalThis.fetch;
const originalInternalToken = process.env.INTERNAL_SERVICE_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalInternalToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
  else process.env.INTERNAL_SERVICE_TOKEN = originalInternalToken;
});

describe('document restore gateway route', () => {
  test('forwards authenticated scope and preserves quota conflicts for restore UI', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    let upstreamUrl = '';
    let upstreamMethod = '';
    let upstreamHeaders: Headers | null = null;
    globalThis.fetch = (async (input, init) => {
      upstreamUrl = String(input);
      upstreamMethod = init?.method || 'GET';
      upstreamHeaders = new Headers(init?.headers);
      return Response.json(
        { detail: 'Workspace document quota prevents restoration.' },
        { status: 409 },
      );
    }) as typeof fetch;

    const app = Fastify({ logger: false });
    app.addHook('preHandler', async (request) => {
      request.user = {
        userId: 'user-proof',
        tenantId: 'tenant-proof',
        roles: ['member'],
        tokenBudget: 1000,
        authType: 'session',
        scopes: [],
      };
    });
    await app.register(documentRoutes);

    try {
      const response = await app.inject({
        method: 'POST',
        url: '/api/documents/00000000-0000-4000-8000-000000000001/restore',
      });

      expect(response.statusCode).toBe(409);
      expect(response.json().detail).toBe('Workspace document quota prevents restoration.');
      expect(upstreamMethod).toBe('POST');
      expect(new URL(upstreamUrl).pathname).toBe(
        '/documents/00000000-0000-4000-8000-000000000001/restore',
      );
      expect(upstreamHeaders?.get('x-certus-tenant-id')).toBe('tenant-proof');
      expect(upstreamHeaders?.get('x-certus-user-id')).toBe('user-proof');
      expect(upstreamHeaders?.get('x-internal-service-token')).toBe(
        'test-internal-service-token-32-characters',
      );
    } finally {
      await app.close();
    }
  });
});
