import { FastifyPluginAsync } from 'fastify';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import {
  clientDisconnectSignal,
  observedInternalServiceFetch,
} from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const traceRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get<{
    Querystring: { limit?: string; cursor?: string; q?: string; model?: string; status?: string };
  }>('/api/traces', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(request.query)) {
      if (typeof value === 'string' && value) query.set(key, value);
    }
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/traces?${query.toString()}`,
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
        'Trace history is temporarily unavailable.',
        error,
      );
    }
  });

  fastify.get<{ Params: { runId: string } }>('/api/traces/:runId', async (request, reply) => {
    const { runId } = request.params;
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/traces/${encodeURIComponent(runId)}`,
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
        'Trace details are temporarily unavailable.',
        error,
      );
    }
  });

  fastify.post<{ Params: { runId: string }; Body: any }>('/api/traces/:runId/replay', async (request, reply) => {
    const { runId } = request.params;
    const { tenantId, userId } = requireAuthContext(request);
    const signal = clientDisconnectSignal(request, reply);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/traces/${encodeURIComponent(runId)}/replay`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal,
          body: JSON.stringify(request.body || {}),
        }, { tenantId, userId }, { profile: 'processing' });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Trace replay is temporarily unavailable.',
        error,
      );
    }
  });
};
