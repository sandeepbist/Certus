import { FastifyPluginAsync } from 'fastify';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const analyticsRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get<{ Querystring: { days?: string } }>('/api/analytics/usage', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const days = Number(request.query.days ?? 30);
    if (!Number.isInteger(days) || days < 7 || days > 90) {
      return reply.status(400).send({
        error: 'InvalidRequest',
        message: 'days must be an integer between 7 and 90.',
      });
    }
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/analytics/usage?days=${days}`,
          {},
          { tenantId, userId },
        );
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Usage analytics are temporarily unavailable.',
        error,
      );
    }
  });
};
