import { FastifyPluginAsync } from 'fastify';
import { mcpBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const MCP_TOOLS_URL = process.env.MCP_TOOLS_URL || 'http://localhost:8003';

export const toolRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get('/api/tools/list', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await mcpBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          mcpBreaker,
          `${MCP_TOOLS_URL}/tools/list`,
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
        'mcp-tools',
        'The tool service is temporarily unavailable.',
        error,
      );
    }
  });

  fastify.post('/api/tools/execute', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const payload = request.body as Record<string, unknown>;
    try {
      return await mcpBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(mcpBreaker, `${MCP_TOOLS_URL}/tools/execute`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }, { tenantId, userId });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'mcp-tools',
        'The tool service is temporarily unavailable.',
        error,
      );
    }
  });
};
