import { FastifyPluginAsync, FastifyReply, FastifyRequest } from 'fastify';

import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';


const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';
const OPERATOR_ROLES = new Set(['owner', 'admin']);

export function isEmbeddingGenerationOperator(request: FastifyRequest): boolean {
  const context = requireAuthContext(request);
  if (!context.roles.some((role) => OPERATOR_ROLES.has(role.toLowerCase()))) {
    return false;
  }
  return context.authType !== 'api_key' || context.scopes.includes('admin');
}

function rejectNonOperator(reply: FastifyReply) {
  return reply.status(403).send({
    error: 'EmbeddingGenerationOperatorRequired',
    message: 'Workspace owner or administrator access is required.',
  });
}

export const embeddingGenerationRoutes: FastifyPluginAsync = async (fastify) => {
  async function proxy(
    request: FastifyRequest,
    reply: FastifyReply,
    path: string,
    init: RequestInit = {},
    processing = false,
  ) {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const response = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}${path}`,
          init,
          { tenantId, userId },
          processing ? { profile: 'processing' } : {},
        );
        return reply.status(response.status).send(await response.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Embedding generation controls are temporarily unavailable.',
        error,
      );
    }
  }

  fastify.get('/api/embedding-generations', async (request, reply) => {
    const query = new URLSearchParams(
      request.query as Record<string, string>,
    ).toString();
    return proxy(
      request,
      reply,
      `/embedding-generations${query ? `?${query}` : ''}`,
    );
  });

  fastify.get<{ Params: { id: string } }>(
    '/api/embedding-generations/:id',
    async (request, reply) => proxy(
      request,
      reply,
      `/embedding-generations/${encodeURIComponent(request.params.id)}`,
    ),
  );

  fastify.post('/api/embedding-generations', async (request, reply) => {
    if (!isEmbeddingGenerationOperator(request)) return rejectNonOperator(reply);
    return proxy(
      request,
      reply,
      '/embedding-generations',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request.body || {}),
      },
      true,
    );
  });

  for (const action of ['cancel', 'rollback'] as const) {
    fastify.post<{ Params: { id: string } }>(
      `/api/embedding-generations/:id/${action}`,
      async (request, reply) => {
        if (!isEmbeddingGenerationOperator(request)) {
          return rejectNonOperator(reply);
        }
        return proxy(
          request,
          reply,
          `/embedding-generations/${encodeURIComponent(request.params.id)}/${action}`,
          { method: 'POST' },
          true,
        );
      },
    );
  }
};
