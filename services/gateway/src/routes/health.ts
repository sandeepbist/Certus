import { FastifyPluginAsync } from 'fastify';
import { ingestionBreaker, orchestrationBreaker, mcpBreaker } from '../utils/circuitBreaker.js';
import { configuredReadinessTargets, probeReadinessTargets } from '../utils/readiness.js';

export const healthRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get('/health', async (_request, reply) => {
    reply.header('Cache-Control', 'no-store');
    return reply.send({
      status: 'ok',
      service: 'gateway',
      version: '0.1.0',
      timestamp: new Date().toISOString(),
    });
  });

  fastify.get('/health/ready', async (_request, reply) => {
    const [dependencies, rateLimitStorageReady] = await Promise.all([
      probeReadinessTargets(configuredReadinessTargets()),
      fastify.checkRateLimitStorage().catch(() => false),
    ]);
    const checks = {
      redis: rateLimitStorageReady ? 'ready' : 'not_ready',
      ...dependencies,
    } as const;
    const ready = Object.values(checks).every((status) => status === 'ready');

    reply.header('Cache-Control', 'no-store');
    return reply.status(ready ? 200 : 503).send({
      status: ready ? 'ready' : 'not_ready',
      checks,
      circuitBreakers: {
        ingestion: ingestionBreaker.getState(),
        orchestration: orchestrationBreaker.getState(),
        mcp: mcpBreaker.getState(),
      },
      timestamp: new Date().toISOString(),
    });
  });
};
