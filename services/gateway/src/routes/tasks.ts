import { FastifyPluginAsync } from 'fastify';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const taskRoutes: FastifyPluginAsync = async (fastify) => {
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
        'The task service is temporarily unavailable.',
        error,
      );
    }
  };

  fastify.get('/api/tasks', async (request, reply) => {
    const query = new URLSearchParams(request.query as Record<string, string>).toString();
    return proxy(request, reply, `/tasks${query ? `?${query}` : ''}`);
  });

  fastify.post('/api/tasks', async (request, reply) => proxy(
    request,
    reply,
    '/tasks',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body || {}),
    },
  ));

  fastify.get<{ Params: { id: string } }>('/api/tasks/:id', async (request, reply) => (
    proxy(request, reply, `/tasks/${encodeURIComponent(request.params.id)}`)
  ));

  fastify.patch<{ Params: { id: string } }>('/api/tasks/:id', async (request, reply) => proxy(
    request,
    reply,
    `/tasks/${encodeURIComponent(request.params.id)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body || {}),
    },
  ));

  fastify.delete<{ Params: { id: string } }>('/api/tasks/:id', async (request, reply) => proxy(
    request,
    reply,
    `/tasks/${encodeURIComponent(request.params.id)}`,
    { method: 'DELETE' },
  ));
};
