import { afterEach, beforeEach, describe, expect, test } from 'bun:test';
import fastify, { type FastifyInstance } from 'fastify';

import rateLimiterPlugin from '../../services/gateway/src/plugins/rateLimiter';

const originalEnvironment = {
  nodeEnv: process.env.NODE_ENV,
  redisUrl: process.env.REDIS_URL,
  tenantLimit: process.env.RATE_LIMIT_REQUESTS_PER_MINUTE,
  edgeLimit: process.env.RATE_LIMIT_IP_REQUESTS_PER_MINUTE,
};
const liveRedisTest = process.env.TEST_REDIS_URL ? test : test.skip;

async function limitedServer(): Promise<FastifyInstance> {
  const app = fastify({ logger: false });
  app.decorateRequest('user', undefined);
  app.addHook('preHandler', async (request) => {
    const tenantId = String(request.headers['x-test-tenant'] || 'tenant-a');
    request.user = {
      userId: 'user-a',
      tenantId,
      roles: ['owner'],
      tokenBudget: 10,
      authType: 'session',
      scopes: ['read', 'write', 'admin'],
    };
  });
  await app.register(rateLimiterPlugin);
  app.get('/health', async () => ({ status: 'ok' }));
  app.get('/limited', async () => ({ status: 'ok' }));
  return app;
}

describe('gateway rate limiter plugin', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'test';
    delete process.env.REDIS_URL;
    process.env.RATE_LIMIT_REQUESTS_PER_MINUTE = '2';
    process.env.RATE_LIMIT_IP_REQUESTS_PER_MINUTE = '4';
  });

  afterEach(() => {
    for (const [name, value] of [
      ['NODE_ENV', originalEnvironment.nodeEnv],
      ['REDIS_URL', originalEnvironment.redisUrl],
      ['RATE_LIMIT_REQUESTS_PER_MINUTE', originalEnvironment.tenantLimit],
      ['RATE_LIMIT_IP_REQUESTS_PER_MINUTE', originalEnvironment.edgeLimit],
    ] as const) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  test('enforces independent tenant buckets and publishes remaining capacity', async () => {
    const app = await limitedServer();
    try {
      const first = await app.inject({ method: 'GET', url: '/limited' });
      const second = await app.inject({ method: 'GET', url: '/limited' });
      const blocked = await app.inject({ method: 'GET', url: '/limited' });
      const otherTenant = await app.inject({
        method: 'GET',
        url: '/limited',
        headers: { 'x-test-tenant': 'tenant-b' },
      });

      expect(first.statusCode).toBe(200);
      expect(first.headers['x-ratelimit-limit']).toBe('2');
      expect(first.headers['x-ratelimit-remaining']).toBe('1');
      expect(second.statusCode).toBe(200);
      expect(second.headers['x-ratelimit-remaining']).toBe('0');
      expect(blocked.statusCode).toBe(429);
      expect(blocked.json()).toMatchObject({ error: 'Too Many Requests' });
      expect(otherTenant.statusCode).toBe(200);
    } finally {
      await app.close();
    }
  });

  test('protects the unauthenticated edge while excluding health probes', async () => {
    process.env.RATE_LIMIT_REQUESTS_PER_MINUTE = '10';
    process.env.RATE_LIMIT_IP_REQUESTS_PER_MINUTE = '2';
    const app = await limitedServer();
    try {
      for (let index = 0; index < 5; index += 1) {
        expect((await app.inject({ method: 'GET', url: '/health' })).statusCode).toBe(200);
      }
      expect((await app.inject({ method: 'GET', url: '/limited' })).statusCode).toBe(200);
      expect((await app.inject({ method: 'GET', url: '/limited' })).statusCode).toBe(200);
      const blocked = await app.inject({ method: 'GET', url: '/limited' });
      expect(blocked.statusCode).toBe(429);
      expect(blocked.headers['retry-after']).toBeDefined();
    } finally {
      await app.close();
    }
  });

  test('requires shared storage in production', async () => {
    process.env.NODE_ENV = 'production';
    delete process.env.REDIS_URL;
    const app = fastify({ logger: false });
    try {
      await expect(app.register(rateLimiterPlugin).ready()).rejects.toThrow(
        'REDIS_URL is required for production rate limiting',
      );
    } finally {
      await app.close();
    }
  });

  liveRedisTest('shares tenant enforcement across gateway instances through Redis', async () => {
    process.env.REDIS_URL = process.env.TEST_REDIS_URL;
    process.env.RATE_LIMIT_REQUESTS_PER_MINUTE = '2';
    process.env.RATE_LIMIT_IP_REQUESTS_PER_MINUTE = '20';
    const firstGateway = await limitedServer();
    const secondGateway = await limitedServer();
    try {
      expect(await firstGateway.checkRateLimitStorage()).toBe(true);
      expect(await secondGateway.checkRateLimitStorage()).toBe(true);
      expect((await firstGateway.inject({ method: 'GET', url: '/limited' })).statusCode).toBe(200);
      expect((await secondGateway.inject({ method: 'GET', url: '/limited' })).statusCode).toBe(200);
      expect((await firstGateway.inject({ method: 'GET', url: '/limited' })).statusCode).toBe(429);
    } finally {
      await Promise.all([firstGateway.close(), secondGateway.close()]);
    }
  });
});
