import { FastifyPluginAsync } from 'fastify';
import fp from 'fastify-plugin';
import { FixedWindowRateLimiter } from '../utils/fixedWindowRateLimiter.js';
import type { RateLimitResult } from '../utils/fixedWindowRateLimiter.js';

declare module 'fastify' {
  interface FastifyInstance {
    checkRequestRate(tenantId: string): Promise<RateLimitResult>;
    checkRateLimitStorage(): Promise<boolean>;
  }
}

const rateLimiterPlugin: FastifyPluginAsync = async (fastify) => {
  const requestsPerMinute = Number(process.env.RATE_LIMIT_REQUESTS_PER_MINUTE || 100);
  if (!Number.isSafeInteger(requestsPerMinute) || requestsPerMinute < 1 || requestsPerMinute > 100_000) {
    throw new Error('RATE_LIMIT_REQUESTS_PER_MINUTE must be an integer between 1 and 100000.');
  }

  const limiter = new FixedWindowRateLimiter(process.env.REDIS_URL, {
    redisRequired: process.env.NODE_ENV === 'production',
  });
  await limiter.start();
  fastify.decorate(
    'checkRequestRate',
    (tenantId: string) => limiter.checkRequestRate(tenantId, requestsPerMinute),
  );
  fastify.decorate('checkRateLimitStorage', () => limiter.isReady());
  fastify.addHook('onClose', async () => limiter.stop());

  fastify.addHook('preHandler', async (request, reply) => {
    // Skip health checks
    if (request.url.startsWith('/health')) {
      return;
    }

    const tenantId = request.user?.tenantId || 'anonymous';
    const { allowed, available, remaining, reset } = await fastify.checkRequestRate(tenantId);

    reply.header('X-RateLimit-Limit', requestsPerMinute);
    reply.header('X-RateLimit-Remaining', remaining);
    reply.header('X-RateLimit-Reset', reset);

    if (!available) {
      request.log.error('Shared rate-limit storage is unavailable');
      reply.header('Retry-After', 1);
      return reply.status(503).send({
        error: 'RateLimitUnavailable',
        message: 'Request limits could not be verified. Please try again.',
      });
    }

    if (request.user?.userId) {
      reply.header('X-TokenBudget-Remaining', request.user.tokenBudget);

      const requestPath = request.url.split('?', 1)[0];
      if (
        request.user.tokenBudget <= 0
        && (requestPath === '/api/chat/query' || requestPath === '/ws/chat')
      ) {
        return reply.status(429).send({
          error: 'Budget Exceeded',
          message: 'The daily agent token budget is exhausted. Try again after 00:00 UTC.',
        });
      }
    }

    if (!allowed) {
      const retryAfter = Math.max(1, reset - Math.floor(Date.now() / 1000));
      reply.header('Retry-After', retryAfter);
      return reply.status(429).send({
        error: 'Too Many Requests',
        message: 'Rate limit exceeded. Please slow down.',
        retryAfter,
      });
    }
  });
};

export default fp(rateLimiterPlugin, { name: 'certus-rate-limiter' });
