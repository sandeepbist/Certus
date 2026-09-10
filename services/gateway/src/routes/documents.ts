import { FastifyPluginAsync } from 'fastify';
import { randomUUID } from 'node:crypto';
import { Readable } from 'node:stream';
import type { ReadableStream as NodeReadableStream } from 'node:stream/web';
import { ingestionBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import {
  clientDisconnectSignal,
  observedInternalServiceFetch,
} from '../utils/internalService.js';
import {
  BoundedUploadStream,
  MAX_MULTIPART_ENVELOPE_BYTES,
  uploadEnvelopeIsTooLarge,
} from '../utils/boundedUploadStream.js';

const INGESTION_SERVICE_URL = process.env.INGESTION_SERVICE_URL || 'http://localhost:8001';

interface DocumentListQuery {
  limit?: string;
  cursor?: string;
  search?: string;
  status?: string;
  tag?: string;
  source_type?: string;
}

interface RechunkBody {
  strategy?: 'token' | 'sentence' | 'recursive';
}

interface DocumentDetailQuery {
  version?: string;
  chunk_limit?: string;
  chunk_offset?: string;
}

interface OriginalDownloadQuery {
  version?: string;
  disposition?: 'attachment' | 'inline';
}

async function readUpstreamPayload(response: Response): Promise<Record<string, unknown>> {
  try {
    return await response.json() as Record<string, unknown>;
  } catch {
    return { detail: 'The document service returned an invalid response.' };
  }
}

export const documentRoutes: FastifyPluginAsync = async (fastify) => {
  // List documents
  fastify.get<{ Querystring: DocumentListQuery }>('/api/documents', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);

    try {
      return await ingestionBreaker.execute(async () => {
        const query = new URLSearchParams();
        for (const field of ['limit', 'cursor', 'search', 'status', 'tag', 'source_type'] as const) {
          const value = request.query[field];
          if (value !== undefined && value !== '') query.set(field, value);
        }
        const res = await observedInternalServiceFetch(
          ingestionBreaker,
          `${INGESTION_SERVICE_URL}/documents?${query.toString()}`,
          {},
          { tenantId, userId },
        );
        const payload = await readUpstreamPayload(res);
        return reply.status(res.status).send(payload);
      });
    } catch {
      return reply.status(503).send({
        error: 'Service Unavailable',
        message: 'The document service is temporarily unavailable.',
      });
    }
  });

  // Get single document details
  fastify.get<{ Params: { id: string }; Querystring: DocumentDetailQuery }>(
    '/api/documents/:id',
    async (request, reply) => {
      const { id } = request.params;
      const { tenantId, userId } = requireAuthContext(request);
      try {
        return await ingestionBreaker.execute(async () => {
          const query = new URLSearchParams();
          for (const field of ['version', 'chunk_limit', 'chunk_offset'] as const) {
            const value = request.query[field];
            if (value !== undefined && value !== '') query.set(field, value);
          }
          const res = await observedInternalServiceFetch(
            ingestionBreaker,
            `${INGESTION_SERVICE_URL}/documents/${encodeURIComponent(id)}?${query.toString()}`,
            {},
            { tenantId, userId },
          );
          const payload = await readUpstreamPayload(res);
          return reply.status(res.status).send(payload);
        });
      } catch {
        return reply.status(503).send({
          error: 'Service Unavailable',
          message: 'The document service is temporarily unavailable.',
        });
      }
    },
  );

  fastify.get<{ Params: { id: string; chunkId: string } }>(
    '/api/documents/:id/evidence/:chunkId',
    async (request, reply) => {
      const { id, chunkId } = request.params;
      const { tenantId, userId } = requireAuthContext(request);
      try {
        return await ingestionBreaker.execute(async () => {
          const res = await observedInternalServiceFetch(
            ingestionBreaker,
            `${INGESTION_SERVICE_URL}/documents/${encodeURIComponent(id)}/evidence/${encodeURIComponent(chunkId)}`,
            {},
            { tenantId, userId },
          );
          for (const headerName of ['cache-control', 'x-content-type-options']) {
            const value = res.headers.get(headerName);
            if (value) reply.header(headerName, value);
          }
          const payload = await readUpstreamPayload(res);
          return reply.status(res.status).send(payload);
        });
      } catch {
        return reply.status(503).send({
          error: 'Service Unavailable',
          message: 'The evidence resolver is temporarily unavailable.',
        });
      }
    },
  );

  fastify.get<{ Params: { id: string }; Querystring: OriginalDownloadQuery }>(
    '/api/documents/:id/original',
    async (request, reply) => {
      const { id } = request.params;
      const { tenantId, userId } = requireAuthContext(request);
      const signal = clientDisconnectSignal(request, reply);
      try {
        return await ingestionBreaker.execute(async () => {
          const query = new URLSearchParams();
          if (request.query.version) query.set('version', request.query.version);
          if (request.query.disposition) query.set('disposition', request.query.disposition);
          const res = await observedInternalServiceFetch(
            ingestionBreaker,
            `${INGESTION_SERVICE_URL}/documents/${encodeURIComponent(id)}/original?${query.toString()}`,
            { signal },
            { tenantId, userId },
            { profile: 'processing' },
          );
          if (!res.ok) {
            const payload = await readUpstreamPayload(res);
            return reply.status(res.status).send(payload);
          }
          if (!res.body) {
            return reply.status(502).send({
              error: 'InvalidResponse',
              message: 'The document service returned an empty original.',
            });
          }
          for (const headerName of [
            'content-type',
            'content-length',
            'content-disposition',
            'content-digest',
            'cache-control',
            'x-content-type-options',
          ]) {
            const value = res.headers.get(headerName);
            if (value) reply.header(headerName, value);
          }
          return reply.send(Readable.fromWeb(
            res.body as unknown as NodeReadableStream<Uint8Array>,
          ));
        });
      } catch {
        return reply.status(503).send({
          error: 'Service Unavailable',
          message: 'The exact original is temporarily unavailable.',
        });
      }
    },
  );

  fastify.post<{ Params: { id: string }; Body: RechunkBody }>(
    '/api/documents/:id/rechunk',
    async (request, reply) => {
      const { id } = request.params;
      const { tenantId, userId } = requireAuthContext(request);
      const signal = clientDisconnectSignal(request, reply);
      try {
        return await ingestionBreaker.execute(async () => {
          const res = await observedInternalServiceFetch(
            ingestionBreaker,
            `${INGESTION_SERVICE_URL}/documents/${encodeURIComponent(id)}/rechunk`,
            {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              signal,
              body: JSON.stringify({ strategy: request.body?.strategy || 'token' }),
            },
            { tenantId, userId },
            { profile: 'processing' },
          );
          const payload = await readUpstreamPayload(res);
          return reply.status(res.status).send(payload);
        });
      } catch {
        return reply.status(503).send({
          error: 'Service Unavailable',
          message: 'The document service is temporarily unavailable.',
        });
      }
    },
  );

  fastify.delete<{ Params: { id: string } }>('/api/documents/:id', async (request, reply) => {
    const { id } = request.params;
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await ingestionBreaker.execute(async () => {
        const res = await observedInternalServiceFetch(
          ingestionBreaker,
          `${INGESTION_SERVICE_URL}/documents/${encodeURIComponent(id)}`,
          { method: 'DELETE' },
          { tenantId, userId },
        );
        const payload = await readUpstreamPayload(res);
        return reply.status(res.status).send(payload);
      });
    } catch {
      return reply.status(503).send({
        error: 'Service Unavailable',
        message: 'The document service is temporarily unavailable.',
      });
    }
  });

  // Document upload endpoint (forwards to Ingestion service)
  fastify.post('/api/documents/upload', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    const signal = clientDisconnectSignal(request, reply);
    try {
      return await ingestionBreaker.execute(async () => {
        const contentType = request.headers['content-type'] || '';
        const incomingIdempotencyKey = request.headers['idempotency-key'];
        const idempotencyKey = (
          Array.isArray(incomingIdempotencyKey) ? incomingIdempotencyKey[0] : incomingIdempotencyKey
        ) || randomUUID();
        
        if (contentType.includes('multipart/form-data')) {
          const contentLength = Number(request.headers['content-length']);
          if (
            Number.isFinite(contentLength)
            && contentLength > MAX_MULTIPART_ENVELOPE_BYTES
          ) {
            return reply.status(413).send({
              error: 'Payload Too Large',
              message: 'Files must be 50 MB or smaller.',
            });
          }

          const headers = new Headers({
            'content-type': contentType,
            'idempotency-key': idempotencyKey,
          });
          if (Number.isFinite(contentLength) && contentLength >= 0) {
            headers.set('content-length', String(contentLength));
          }
          const uploadBody = request.raw.pipe(new BoundedUploadStream());
          const streamingRequest: RequestInit & { duplex: 'half' } = {
            method: 'POST',
            headers,
            body: uploadBody as unknown as RequestInit['body'],
            duplex: 'half',
            signal,
          };
          const res = await observedInternalServiceFetch(
            ingestionBreaker,
            `${INGESTION_SERVICE_URL}/ingest`,
            streamingRequest,
            { tenantId, userId },
            { profile: 'processing' },
          );
          const json = await readUpstreamPayload(res);
          return reply.status(res.status).send(json);
        }

        // JSON text payload fallback
        const body: any = request.body || {};
        const filename = body.filename || 'document.txt';
        const content = body.content || '';
        const strategy = body.chunkStrategy || 'token';

        const formData = new FormData();
        const blob = new Blob([content], { type: 'text/plain' });
        formData.append('file', blob, filename);
        formData.append('chunk_strategy', strategy);
        if (body.tags) formData.append('tags', body.tags);
        if (body.replaceDocumentId) formData.append('replace_document_id', body.replaceDocumentId);
        if (body.sourceTime) formData.append('source_time', body.sourceTime);

        const res = await observedInternalServiceFetch(ingestionBreaker, `${INGESTION_SERVICE_URL}/ingest`, {
          method: 'POST',
          headers: { 'idempotency-key': idempotencyKey },
          body: formData,
          signal,
        }, { tenantId, userId }, { profile: 'processing' });
        const json = await readUpstreamPayload(res);
        return reply.status(res.status).send(json);
      });
    } catch (err: unknown) {
      if (
        err instanceof fastify.multipartErrors.RequestFileTooLargeError
        || uploadEnvelopeIsTooLarge(err)
      ) {
        return reply.status(413).send({
          error: 'Payload Too Large',
          message: 'Files must be 50 MB or smaller.',
        });
      }
      return reply.status(503).send({
        error: 'Service Unavailable',
        message: 'The ingestion service is temporarily unavailable.',
      });
    }
  });
};
