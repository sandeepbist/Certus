import { FastifyPluginAsync } from 'fastify';
import fp from 'fastify-plugin';
import cors from '@fastify/cors';

const corsPlugin: FastifyPluginAsync = async (fastify) => {
  const allowedOrigins = new Set(
    (process.env.CORS_ALLOWED_ORIGINS || process.env.APP_URL || 'http://localhost:3000')
      .split(',')
      .map((origin) => origin.trim())
      .filter(Boolean),
  );

  await fastify.register(cors, {
    origin: (origin, cb) => {
      // Requests without an Origin are non-browser service/CLI requests.
      if (!origin) {
        cb(null, true);
        return;
      }
      if (allowedOrigins.has(origin)) {
        cb(null, true);
      } else {
        const error = Object.assign(new Error('Origin is not allowed'), { statusCode: 403 });
        cb(error, false);
      }
    },
    credentials: true,
    methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'X-Requested-With', 'X-Trace-Id'],
    exposedHeaders: [
      'X-Request-Id',
      'X-Trace-Id',
      'X-RateLimit-Remaining',
      'X-RateLimit-Reset',
      'X-TokenBudget-Remaining',
      'X-RateLimit-Limit',
    ],
  });
};

export default fp(corsPlugin, { name: 'certus-cors' });
