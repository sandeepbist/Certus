import { FastifyPluginAsync } from 'fastify';

import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const notificationRoutes: FastifyPluginAsync = async (fastify) => {
  const proxy = async (
    request: any,
    reply: any,
    path: string,
    init: RequestInit = {},
  ) => {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const response = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}${path}`,
          init,
          { tenantId, userId },
        );
        const body = await response.json();
        return reply.status(response.status).send(body);
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'The notification service is temporarily unavailable.',
        error,
      );
    }
  };

  fastify.get('/api/notifications', async (request, reply) => {
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    return proxy(request, reply, `/notifications${query ? `?${query}` : ''}`);
  });

  fastify.patch<{ Params: { id: string } }>('/api/notifications/:id/read', async (request, reply) => (
    proxy(request, reply, `/notifications/${encodeURIComponent(request.params.id)}/read`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body || {}),
    })
  ));

  fastify.post('/api/notifications/read-all', async (request, reply) => (
    proxy(request, reply, '/notifications/read-all', { method: 'POST' })
  ));

  fastify.post('/api/notifications/bulk', async (request, reply) => (
    proxy(request, reply, '/notifications/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body || {}),
    })
  ));

  fastify.delete<{ Params: { id: string } }>('/api/notifications/:id', async (request, reply) => (
    proxy(request, reply, `/notifications/${encodeURIComponent(request.params.id)}`, { method: 'DELETE' })
  ));
};
