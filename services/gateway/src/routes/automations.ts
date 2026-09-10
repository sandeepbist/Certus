import { FastifyPluginAsync } from 'fastify';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const automationRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get('/api/automations', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/automations${query ? `?${query}` : ''}`,
          {},
          { tenantId, userId },
        );
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Automations are temporarily unavailable.', error);
    }
  });

  fastify.post('/api/automations', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const payload = request.body as Record<string, unknown>;
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/automations`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }, { tenantId, userId });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Automations are temporarily unavailable.', error);
    }
  });

  fastify.delete<{ Params: { id: string } }>('/api/automations/:id', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/automations/${encodeURIComponent(request.params.id)}`,
          { method: 'DELETE' },
          { tenantId, userId },
        );
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Automations are temporarily unavailable.', error);
    }
  });

  fastify.patch<{ Params: { id: string } }>('/api/automations/:id', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/automations/${encodeURIComponent(request.params.id)}`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(request.body || {}),
          },
          { tenantId, userId },
        );
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Automations are temporarily unavailable.', error);
    }
  });

  fastify.get<{ Params: { id: string } }>('/api/automations/:id/history', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/automations/${encodeURIComponent(request.params.id)}/history${query ? `?${query}` : ''}`,
          {},
          { tenantId, userId },
        );
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(request, reply, 'orchestration', 'Automation history is temporarily unavailable.', error);
    }
  });
};
