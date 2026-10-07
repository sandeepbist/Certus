import { afterEach, describe, expect, test } from 'bun:test';
import Fastify from 'fastify';

import { exportRoutes } from '../../services/gateway/src/routes/export';


const originalFetch = globalThis.fetch;
const originalInternalToken = process.env.INTERNAL_SERVICE_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalInternalToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
  else process.env.INTERNAL_SERVICE_TOKEN = originalInternalToken;
});

describe('export gateway streaming', () => {
  test('forwards the originals choice and rejects invalid flags', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    let upstreamUrl: URL | undefined;
    globalThis.fetch = (async (input) => {
      upstreamUrl = new URL(String(input));
      return Response.json({ status: 'ready' });
    }) as typeof fetch;
    const app = Fastify({ logger: false });
    app.addHook('preHandler', async (request) => {
      request.user = {
        userId: 'user-proof', tenantId: 'tenant-proof', roles: ['member'],
        tokenBudget: 1_000, authType: 'session', scopes: [],
      };
    });
    await app.register(exportRoutes);
    try {
      const included = await app.inject({ method: 'POST', url: '/api/export?include_originals=true' });
      expect(included.statusCode).toBe(200);
      expect(upstreamUrl?.searchParams.get('include_originals')).toBe('true');
      const metadataOnly = await app.inject({ method: 'POST', url: '/api/export' });
      expect(metadataOnly.statusCode).toBe(200);
      expect(upstreamUrl?.searchParams.get('include_originals')).toBe('false');
      const invalid = await app.inject({ method: 'POST', url: '/api/export?include_originals=maybe' });
      expect(invalid.statusCode).toBe(400);
    } finally {
      await app.close();
    }
  });

  test('forwards an archive stream without allocating an array buffer', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    let upstreamSignal: AbortSignal | null | undefined;
    globalThis.fetch = (async (_input, init) => {
      upstreamSignal = init?.signal;
      const response = new Response(new Uint8Array([0x50, 0x4b, 0x03, 0x04]), {
        status: 200,
        headers: {
          'content-type': 'application/zip',
          'content-length': '4',
          'content-disposition': 'attachment; filename="proof.zip"',
        },
      });
      response.arrayBuffer = async () => {
        throw new Error('Gateway must not buffer the archive');
      };
      return response;
    }) as typeof fetch;

    const app = Fastify({ logger: false });
    app.addHook('preHandler', async (request) => {
      request.user = {
        userId: 'user-proof',
        tenantId: 'tenant-proof',
        roles: ['member'],
        tokenBudget: 1_000,
        authType: 'session',
        scopes: [],
      };
    });
    await app.register(exportRoutes);

    const response = await app.inject({
      method: 'GET',
      url: '/api/export/00000000-0000-4000-8000-000000000001',
    });
    await app.close();

    expect(response.statusCode).toBe(200);
    expect(response.rawPayload).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
    expect(response.headers['content-disposition']).toBe('attachment; filename="proof.zip"');
    expect(response.headers['cache-control']).toBe('private, no-store');
    expect(upstreamSignal).toBeInstanceOf(AbortSignal);
  });
});
