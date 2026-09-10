import { FastifyPluginAsync } from 'fastify';
import { Readable } from 'node:stream';
import type { ReadableStream as NodeReadableStream } from 'node:stream/web';
import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import {
  clientDisconnectSignal,
  observedInternalServiceFetch,
} from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';

export const exportRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.post('/api/export', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const signal = clientDisconnectSignal(request, reply);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(orchestrationBreaker, `${ORCHESTRATION_SERVICE_URL}/export`, {
          method: 'POST',
          signal,
        }, { tenantId, userId }, { profile: 'processing' });
        if (res.ok) return await res.json();
        return reply.status(res.status).send(await res.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Data export is temporarily unavailable.',
        error,
      );
    }
  });

  fastify.get<{ Params: { exportId: string } }>('/api/export/:exportId', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const signal = clientDisconnectSignal(request, reply);
    try {
      return await orchestrationBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/export/${encodeURIComponent(request.params.exportId)}`,
          { signal },
          { tenantId, userId },
          { profile: 'processing' },
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: 'Export download failed.' }));
          return reply.status(res.status).send(body);
        }

        const contentDisposition = res.headers.get('content-disposition');
        const contentLength = res.headers.get('content-length');
        reply.header('Content-Type', res.headers.get('content-type') || 'application/zip');
        reply.header('Cache-Control', 'private, no-store');
        reply.header('X-Content-Type-Options', 'nosniff');
        if (contentDisposition) reply.header('Content-Disposition', contentDisposition);
        if (contentLength) reply.header('Content-Length', contentLength);
        if (!res.body) {
          return reply.status(502).send({
            error: 'InvalidResponse',
            message: 'The export service returned an empty archive.',
          });
        }
        return reply.send(Readable.fromWeb(
          res.body as unknown as NodeReadableStream<Uint8Array>,
        ));
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Export download is temporarily unavailable.',
        error,
      );
    }
  });
};
