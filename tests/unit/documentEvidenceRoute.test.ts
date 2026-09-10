import { afterEach, describe, expect, test } from 'bun:test';
import Fastify from 'fastify';

import { documentRoutes } from '../../services/gateway/src/routes/documents';


const originalFetch = globalThis.fetch;
const originalInternalToken = process.env.INTERNAL_SERVICE_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalInternalToken === undefined) {
    delete process.env.INTERNAL_SERVICE_TOKEN;
  } else {
    process.env.INTERNAL_SERVICE_TOKEN = originalInternalToken;
  }
});

describe('document evidence gateway route', () => {
  test('forwards scoped identity and preserves fail-closed response headers', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    let upstreamUrl = '';
    let upstreamHeaders: Headers | null = null;
    globalThis.fetch = (async (input, init) => {
      upstreamUrl = String(input);
      upstreamHeaders = new Headers(init?.headers);
      return new Response(JSON.stringify({ resolution_status: 'verified' }), {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'cache-control': 'private, no-store',
          'x-content-type-options': 'nosniff',
        },
      });
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

    const response = await app.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-4000-8000-000000000001/evidence/00000000-0000-4000-8000-000000000002',
    });
    await app.close();

    expect(response.statusCode).toBe(200);
    expect(response.headers['cache-control']).toBe('private, no-store');
    expect(response.headers['x-content-type-options']).toBe('nosniff');
    expect(new URL(upstreamUrl).pathname).toBe(
      '/documents/00000000-0000-4000-8000-000000000001/evidence/00000000-0000-4000-8000-000000000002',
    );
    expect(upstreamHeaders?.get('x-certus-tenant-id')).toBe('tenant-proof');
    expect(upstreamHeaders?.get('x-certus-user-id')).toBe('user-proof');
    expect(upstreamHeaders?.get('x-internal-service-token')).toBe(
      'test-internal-service-token-32-characters',
    );
  });

  test('forwards the bounded inline disposition for the private PDF viewer', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    let upstreamUrl = '';
    globalThis.fetch = (async (input) => {
      upstreamUrl = String(input);
      return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
        status: 200,
        headers: {
          'content-type': 'application/pdf',
          'content-length': '4',
          'content-disposition': 'inline; filename="proof.pdf"',
          'content-digest': 'sha-256=:proof:',
          'cache-control': 'private, no-store',
          'x-content-type-options': 'nosniff',
        },
      });
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

    const response = await app.inject({
      method: 'GET',
      url: '/api/documents/00000000-0000-4000-8000-000000000001/original?version=2&disposition=inline',
    });
    await app.close();

    const upstream = new URL(upstreamUrl);
    expect(upstream.searchParams.get('version')).toBe('2');
    expect(upstream.searchParams.get('disposition')).toBe('inline');
    expect(response.headers['content-disposition']).toStartWith('inline;');
    expect(response.headers['cache-control']).toBe('private, no-store');
    expect(response.headers['x-content-type-options']).toBe('nosniff');
  });
});
