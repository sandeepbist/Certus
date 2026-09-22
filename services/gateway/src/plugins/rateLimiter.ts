import rateLimit, { normalizeIP } from '@fastify/rate-limit';
import { FastifyPluginAsync, FastifyRequest } from 'fastify';
import fp from 'fastify-plugin';
import { Redis } from 'ioredis';
import { createHash } from 'node:crypto';

type RateLimitResult = {
  allowed: boolean;
  available: boolean;
  remaining: number;
  reset: number;
};

declare module 'fastify' {
  interface FastifyInstance {
    checkRequestRate(request: FastifyRequest): Promise<RateLimitResult>;
    checkRateLimitStorage(): Promise<boolean>;
  }
}

const healthPaths = new Set(['/health', '/health/ready']);

function tenantRateLimitKey(request: FastifyRequest): string {
  if (!request.user?.tenantId) return `anonymous:${normalizeIP(request.ip)}`;
  const tenantDigest = createHash('sha256').update(request.user.tenantId).digest('hex');
  return `tenant:${tenantDigest}`;
}

function boundedRateLimit(name: string, fallback: number): number {
  const configured = Number(process.env[name] || fallback);
  if (!Number.isSafeInteger(configured) || configured < 1 || configured > 100_000) {
    throw new Error(`${name} must be an integer between 1 and 100000.`);
  }
  return configured;
}

const rateLimiterPlugin: FastifyPluginAsync = async (fastify) => {
  const tenantRequestsPerMinute = boundedRateLimit('RATE_LIMIT_REQUESTS_PER_MINUTE', 100);
  const edgeRequestsPerMinute = boundedRateLimit('RATE_LIMIT_IP_REQUESTS_PER_MINUTE', 300);
  const redisRequired = process.env.NODE_ENV === 'production';
  const redisUrl = process.env.REDIS_URL?.trim();
  if (redisRequired && !redisUrl) {
    throw new Error('REDIS_URL is required for production rate limiting.');
  }

  let redis: Redis | null = null;
  if (redisUrl) {
    const candidate = new Redis(redisUrl, {
      connectTimeout: 5_000,
      maxRetriesPerRequest: 1,
      enableOfflineQueue: false,
      lazyConnect: true,
    });
    candidate.on('error', () => {
      // Readiness and request checks expose storage outages without logging
      // connection details that may contain credentials.
    });
    try {
      await candidate.connect();
      await candidate.ping();
      redis = candidate;
    } catch (error) {
      candidate.disconnect(false);
      if (redisRequired) throw error;
      fastify.log.warn('Shared rate-limit storage unavailable; using bounded local storage');
    }
  }

  await fastify.register(rateLimit, {
    global: true,
    hook: 'onRequest',
    max: edgeRequestsPerMinute,
    timeWindow: 60_000,
    cache: 10_000,
    redis: redis || undefined,
    nameSpace: 'certus-rate-limit:',
    skipOnError: false,
    allowList: (request) => healthPaths.has(request.url.split('?', 1)[0]),
    errorResponseBuilder: (_request, context) => ({
      statusCode: context.statusCode,
      error: 'Too Many Requests',
      message: 'Rate limit exceeded. Please slow down.',
      retryAfter: Math.max(1, Math.ceil(context.ttl / 1_000)),
    }),
  });

  const checkTenantRate = fastify.createRateLimit({
    max: tenantRequestsPerMinute,
    timeWindow: 60_000,
    keyGenerator: tenantRateLimitKey,
  });

  fastify.decorate('checkRequestRate', async (request: FastifyRequest) => {
    try {
      const result = await checkTenantRate(request);
      const now = Math.floor(Date.now() / 1_000);
      if (result.isAllowed) {
        return {
          allowed: true,
          available: true,
          remaining: tenantRequestsPerMinute,
          reset: now + 60,
        };
      }
      return {
        allowed: !result.isExceeded,
        available: true,
        remaining: result.remaining,
        reset: now + result.ttlInSeconds,
      };
    } catch {
      request.log.error('Shared rate-limit storage is unavailable');
      return {
        allowed: false,
        available: false,
        remaining: 0,
        reset: Math.floor(Date.now() / 1_000) + 1,
      };
    }
  });
  fastify.decorate('checkRateLimitStorage', async () => {
    if (!redis) return !redisRequired;
    try {
      return await redis.ping() === 'PONG';
    } catch {
      return false;
    }
  });
  fastify.addHook('onClose', async () => {
    redis?.disconnect(false);
    redis = null;
  });

  fastify.addHook('preHandler', async (request, reply) => {
    const requestPath = request.url.split('?', 1)[0];
    if (healthPaths.has(requestPath)) return;

    const { allowed, available, remaining, reset } = await fastify.checkRequestRate(request);

    reply.header('X-RateLimit-Limit', tenantRequestsPerMinute);
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
