import { FastifyPluginAsync } from 'fastify';

import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const webhookRoutes: FastifyPluginAsync = async (fastify) => {
  const proxy = async (request: any, reply: any, path: string, init: RequestInit = {}) => {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const response = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}${path}`,
          init,
          { tenantId, userId },
        );
        return reply.status(response.status).send(await response.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'The webhook service is temporarily unavailable.',
        error,
      );
    }
  };

  fastify.get('/api/webhooks', async (request, reply) => proxy(request, reply, '/webhooks'));
  fastify.post('/api/webhooks', async (request, reply) => proxy(request, reply, '/webhooks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request.body || {}),
  }));

  fastify.patch<{ Params: { id: string } }>('/api/webhooks/:id', async (request, reply) => proxy(
    request,
    reply,
    `/webhooks/${encodeURIComponent(request.params.id)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body || {}),
    },
  ));

  fastify.post<{ Params: { id: string } }>('/api/webhooks/:id/test', async (request, reply) => proxy(
    request,
    reply,
    `/webhooks/${encodeURIComponent(request.params.id)}/test`,
    { method: 'POST' },
  ));

  fastify.post<{ Params: { id: string } }>('/api/webhooks/:id/rotate-secret', async (request, reply) => proxy(
    request,
    reply,
    `/webhooks/${encodeURIComponent(request.params.id)}/rotate-secret`,
    { method: 'POST' },
  ));

  fastify.get<{ Params: { id: string } }>('/api/webhooks/:id/deliveries', async (request, reply) => {
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    return proxy(
      request,
      reply,
      `/webhooks/${encodeURIComponent(request.params.id)}/deliveries${query ? `?${query}` : ''}`,
    );
  });

  fastify.delete<{ Params: { id: string } }>('/api/webhooks/:id', async (request, reply) => proxy(
    request,
    reply,
    `/webhooks/${encodeURIComponent(request.params.id)}`,
    { method: 'DELETE' },
  ));
};
