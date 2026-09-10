import { afterEach, describe, expect, test } from 'bun:test';
import Fastify from 'fastify';

import { automationRoutes } from '../../services/gateway/src/routes/automations';
import { documentRoutes } from '../../services/gateway/src/routes/documents';
import { memoryRoutes } from '../../services/gateway/src/routes/memories';
import { notificationRoutes } from '../../services/gateway/src/routes/notifications';
import { taskRoutes } from '../../services/gateway/src/routes/tasks';
import { traceRoutes } from '../../services/gateway/src/routes/traces';

const originalFetch = globalThis.fetch;
const originalInternalToken = process.env.INTERNAL_SERVICE_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalInternalToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
  else process.env.INTERNAL_SERVICE_TOKEN = originalInternalToken;
});

describe('bounded collection pagination forwarding', () => {
  test('preserves collection continuation parameters', async () => {
    process.env.INTERNAL_SERVICE_TOKEN = 'test-internal-service-token-32-characters';
    const upstreamUrls: string[] = [];
    globalThis.fetch = (async (input) => {
      upstreamUrls.push(String(input));
      return Response.json({ items: [] });
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
    await app.register(memoryRoutes);
    await app.register(automationRoutes);
    await app.register(documentRoutes);
    await app.register(notificationRoutes);
    await app.register(taskRoutes);
    await app.register(traceRoutes);

    try {
      const pageCursor = '00000000-0000-4000-8000-000000000001';
      const ruleId = '00000000-0000-4000-8000-000000000002';
      expect((await app.inject({
        method: 'GET',
        url: `/api/memories?limit=50&cursor=${pageCursor}`,
      })).statusCode).toBe(200);
      expect((await app.inject({
        method: 'GET',
        url: `/api/automations?limit=25&cursor=${pageCursor}`,
      })).statusCode).toBe(200);
      expect((await app.inject({
        method: 'GET',
        url: `/api/automations/${ruleId}/history?limit=10&cursor=${pageCursor}`,
      })).statusCode).toBe(200);
      expect((await app.inject({
        method: 'GET',
        url: `/api/tasks?limit=50&status=pending&q=proof&cursor=${pageCursor}`,
      })).statusCode).toBe(200);
      expect((await app.inject({
        method: 'GET',
        url: `/api/notifications?limit=25&status=unread&type=task_due&cursor=${pageCursor}`,
      })).statusCode).toBe(200);
      expect((await app.inject({
        method: 'GET',
        url: `/api/traces?limit=25&q=proof&model=proof-model&status=completed&cursor=${pageCursor}`,
      })).statusCode).toBe(200);
      expect((await app.inject({
        method: 'GET',
        url: `/api/documents?limit=50&search=proof&status=ready&tag=research&source_type=pdf&cursor=${pageCursor}`,
      })).statusCode).toBe(200);

      expect(upstreamUrls).toEqual([
        `http://localhost:8002/memories?limit=50&cursor=${pageCursor}`,
        `http://localhost:8002/automations?limit=25&cursor=${pageCursor}`,
        `http://localhost:8002/automations/${ruleId}/history?limit=10&cursor=${pageCursor}`,
        `http://localhost:8002/tasks?limit=50&status=pending&q=proof&cursor=${pageCursor}`,
        `http://localhost:8002/notifications?limit=25&status=unread&type=task_due&cursor=${pageCursor}`,
        `http://localhost:8002/traces?limit=25&q=proof&model=proof-model&status=completed&cursor=${pageCursor}`,
        `http://localhost:8001/documents?limit=50&cursor=${pageCursor}&search=proof&status=ready&tag=research&source_type=pdf`,
      ]);
    } finally {
      await app.close();
    }
  });
});
