import { FastifyPluginAsync } from 'fastify';
import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import {
  clientDisconnectSignal,
  observedInternalServiceFetch,
} from '../utils/internalService.js';

interface ChatRequestBody {
  query: string;
  sessionId?: string;
  model?: string;
  requestId?: string;
  documentIds?: string[];
  versionScope?: string;
}

const selectedDocumentIdsSchema = z.array(z.string().uuid()).max(10);

export function parseSelectedDocumentIds(value: unknown): string[] | null {
  if (value === undefined) return [];
  const parsed = selectedDocumentIdsSchema.safeParse(value);
  if (!parsed.success) return null;
  return [...new Set(parsed.data.map((documentId) => documentId.toLowerCase()))];
}

const selectedVersionScopeSchema = z.enum(['auto', 'all_history', 'current_only']);
const sessionIdSchema = z.string().uuid();

export function parseSelectedVersionScope(value: unknown): z.infer<typeof selectedVersionScopeSchema> | null {
  if (value === undefined) return 'auto';
  const parsed = selectedVersionScopeSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

export function parseChatSessionId(value: unknown, requestId?: string): string | null {
  if (value === undefined) return requestId || randomUUID();
  const parsed = sessionIdSchema.safeParse(value);
  return parsed.success ? parsed.data.toLowerCase() : null;
}

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const chatRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.post<{ Body: ChatRequestBody }>('/api/chat/query', async (request, reply) => {
    const {
      query,
      sessionId: rawSessionId,
      model,
      requestId,
      documentIds: rawDocumentIds,
      versionScope: rawVersionScope,
    } = request.body || {};
    const { tenantId, userId } = requireAuthContext(request);

    if (typeof query !== 'string' || !query.trim()) {
      return reply.status(400).send({
        error: 'Bad Request',
        message: 'Field "query" is required',
      });
    }
    if (query.length > 20_000) {
      return reply.status(413).send({
        error: 'Payload Too Large',
        message: 'Queries must be 20,000 characters or fewer.',
      });
    }
    if (requestId !== undefined && !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(requestId)) {
      return reply.status(400).send({
        error: 'Bad Request',
        message: 'Field "requestId" must be a UUIDv4.',
      });
    }
    const sessionId = parseChatSessionId(rawSessionId, requestId);
    if (sessionId === null) {
      return reply.status(400).send({
        error: 'Bad Request',
        message: 'Field "sessionId" must be a UUID.',
      });
    }
    const documentIds = parseSelectedDocumentIds(rawDocumentIds);
    if (documentIds === null) {
      return reply.status(400).send({
        error: 'Bad Request',
        message: 'Field "documentIds" must contain at most 10 UUIDs.',
      });
    }
    const versionScope = parseSelectedVersionScope(rawVersionScope);
    if (versionScope === null) {
      return reply.status(400).send({
        error: 'Bad Request',
        message: 'Field "versionScope" must be auto, all_history, or current_only.',
      });
    }

    const signal = clientDisconnectSignal(request, reply);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal,
          body: JSON.stringify({
            query: query.trim(),
            session_id: sessionId,
            model,
            request_id: requestId,
            document_ids: documentIds,
            version_scope: versionScope,
          }),
        }, { tenantId, userId }, { profile: 'processing' });

        if (res.ok) {
          return await res.json();
        }

        const body = await res.json().catch(() => ({
          error: 'OrchestrationError',
          message: 'The orchestration service could not process this query.',
        }));
        return reply.status(res.status).send(body);
      });
    } catch (error: any) {
      request.log.error({ err: error }, 'Orchestration request failed');
      return reply.status(503).send({
        error: 'Service Unavailable',
        message: 'The orchestration service is unavailable. Please try again.',
      });
    }
  });
};
