import { FastifyPluginAsync } from 'fastify';

import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const searchRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get('/api/search', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    try {
      return await orchestrationBreaker.execute(async () => {
        const response = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/search${query ? `?${query}` : ''}`,
          {},
          { tenantId, userId },
        );
        return reply.status(response.status).send(await response.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Workspace search is temporarily unavailable.',
        error,
      );
    }
  });
};
