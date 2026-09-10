import { FastifyPluginAsync } from 'fastify';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';


const ORCHESTRATION_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

interface GraphQuery {
  query?: string;
  depth?: string;
  limit?: string;
  entity_type?: string;
}

export const graphRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get<{ Querystring: GraphQuery }>('/api/graph', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const query = new URLSearchParams();
    for (const field of ['query', 'depth', 'limit', 'entity_type'] as const) {
      const value = request.query[field];
      if (value !== undefined && value !== '') query.set(field, value);
    }

    try {
      return await orchestrationBreaker.execute(async () => {
        const response = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_URL}/graph?${query.toString()}`,
          {},
          { tenantId, userId },
        );
        let payload: Record<string, unknown>;
        try {
          payload = await response.json() as Record<string, unknown>;
        } catch {
          payload = { detail: 'The graph service returned an invalid response.' };
        }
        return reply.status(response.status).send(payload);
      });
    } catch {
      return reply.status(503).send({
        error: 'Service Unavailable',
        message: 'The knowledge graph is temporarily unavailable.',
      });
    }
  });
};
