import { FastifyPluginAsync } from 'fastify';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import {
  clientDisconnectSignal,
  observedInternalServiceFetch,
} from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const memoryRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get('/api/memories', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/memories${query ? `?${query}` : ''}`,
          {},
          { tenantId, userId },
        );
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Memories are temporarily unavailable.', error);
    }
  });

  fastify.post('/api/memories', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const payload = request.body as Record<string, unknown>;
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/memories`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }, { tenantId, userId });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Memories are temporarily unavailable.', error);
    }
  });

  fastify.post('/api/memories/reflect', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const signal = clientDisconnectSignal(request, reply);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/memories/reflect`, {
          method: 'POST',
          signal,
        }, { tenantId, userId }, { profile: 'processing' });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Memory reflection is temporarily unavailable.', error);
    }
  });

  fastify.delete<{ Params: { id: string } }>('/api/memories/:id', async (request, reply) => {
    const { id } = request.params;
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/memories/${encodeURIComponent(id)}`, {
          method: 'DELETE',
        }, { tenantId, userId });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Memories are temporarily unavailable.', error);
    }
  });
};
