import type { FastifyRequest } from 'fastify';
import { WebSocket } from 'ws';
import { z } from 'zod';

import { requireAuthContext } from '../utils/authContext.js';
import { internalServiceFetch } from '../utils/internalService.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';
const HEARTBEAT_INTERVAL_MS = 30_000;
const HEARTBEAT_TIMEOUT_MS = 10_000;
const MAX_BUFFERED_BYTES = 1024 * 1024;

const clientMessageSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('ping') }).strict(),
  z.object({
    type: z.literal('query'),
    content: z.string().trim().min(1).max(20_000),
    sessionId: z.string().uuid().optional(),
    model: z.string().trim().min(1).max(100).optional(),
    requestId: z.string().uuid(),
    documentIds: z.array(z.string().uuid()).max(10).optional(),
    versionScope: z.enum(['auto', 'all_history', 'current_only']).optional(),
  }).strict(),
  z.object({
    type: z.literal('cancel'),
    requestId: z.string().uuid(),
  }).strict(),
]);

export function parseSseFrame(frame: string): string | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, ''))
    .join('\n');
  return data || null;
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === 'AbortError';
}

export function handleWebSocketConnection(socket: WebSocket, request: FastifyRequest) {
  const { tenantId, userId } = requireAuthContext(request);
  let activeRequestId: string | null = null;
  let upstreamController: AbortController | null = null;
  let receivedPong = true;
  let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null;

  const sendJson = (payload: unknown) => {
    if (socket.readyState !== WebSocket.OPEN) return false;
    try {
      socket.send(JSON.stringify(payload));
      return true;
    } catch (error) {
      request.log.warn({ err: error }, 'Chat WebSocket send failed');
      upstreamController?.abort();
      return false;
    }
  };

  const waitForWritableSocket = async (signal: AbortSignal) => {
    while (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
      if (signal.aborted || socket.readyState !== WebSocket.OPEN) {
        throw new DOMException('Chat stream was aborted', 'AbortError');
      }
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  };

  const streamQuery = async (payload: Extract<z.infer<typeof clientMessageSchema>, { type: 'query' }>) => {
    if (activeRequestId) {
      sendJson({
        type: 'error',
        code: 'QUERY_IN_PROGRESS',
        request_id: payload.requestId,
        message: 'Wait for the active query to finish or cancel it first.',
      });
      return;
    }

    const rateLimit = await request.server.checkRequestRate(tenantId);
    if (!rateLimit.available) {
      sendJson({
        type: 'error',
        code: 'RATE_LIMIT_UNAVAILABLE',
        request_id: payload.requestId,
        message: 'Request limits could not be verified. Please try again.',
      });
      return;
    }
    if (!rateLimit.allowed) {
      sendJson({
        type: 'error',
        code: 'RATE_LIMITED',
        request_id: payload.requestId,
        retry_after: Math.max(1, rateLimit.reset - Math.floor(Date.now() / 1_000)),
        message: 'Rate limit exceeded. Please slow down.',
      });
      return;
    }

    const controller = new AbortController();
    activeRequestId = payload.requestId;
    upstreamController = controller;
    sendJson({ type: 'stream.started', request_id: payload.requestId });

    try {
      const response = await internalServiceFetch(
        `${ORCHESTRATION_SERVICE_URL}/chat/stream`,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            query: payload.content,
            session_id: payload.sessionId || payload.requestId,
            model: payload.model,
            request_id: payload.requestId,
            document_ids: [...new Set(
              (payload.documentIds || []).map((documentId) => documentId.toLowerCase()),
            )],
            version_scope: payload.versionScope || 'auto',
          }),
          signal: controller.signal,
        },
        { tenantId, userId },
        { profile: 'stream' },
      );

      if (!response.ok || !response.body) {
        sendJson({
          type: 'error',
          code: 'ORCHESTRATION_FAILED',
          request_id: payload.requestId,
          message: `Orchestration failed with status ${response.status}.`,
        });
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() || '';

          for (const frame of frames) {
            const eventJson = parseSseFrame(frame);
            if (!eventJson) continue;
            let event: unknown;
            try {
              event = JSON.parse(eventJson);
            } catch {
              request.log.warn('Orchestration emitted a malformed chat event');
              continue;
            }
            if (typeof event !== 'object' || event === null || Array.isArray(event)) continue;
            await waitForWritableSocket(controller.signal);
            if (!sendJson({ ...event, request_id: payload.requestId })) return;
          }
        }

        buffer += decoder.decode();
        const trailingEvent = parseSseFrame(buffer);
        if (trailingEvent) {
          try {
            const event: unknown = JSON.parse(trailingEvent);
            if (typeof event === 'object' && event !== null && !Array.isArray(event)) {
              await waitForWritableSocket(controller.signal);
              sendJson({ ...event, request_id: payload.requestId });
            }
          } catch {
            request.log.warn('Orchestration emitted a malformed trailing chat event');
          }
        }
      } finally {
        if (controller.signal.aborted) await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    } catch (error) {
      if (isAbortError(error) || controller.signal.aborted) {
        sendJson({
          type: 'stream.cancelled',
          request_id: payload.requestId,
          message: 'The chat stream was cancelled.',
        });
      } else {
        request.log.error({ err: error }, 'WebSocket orchestration stream failed');
        sendJson({
          type: 'error',
          code: 'ORCHESTRATION_UNAVAILABLE',
          request_id: payload.requestId,
          message: 'The orchestration service is unavailable. Please try again.',
        });
      }
    } finally {
      if (activeRequestId === payload.requestId) activeRequestId = null;
      if (upstreamController === controller) upstreamController = null;
    }
  };

  socket.on('message', (data, isBinary) => {
    if (isBinary) {
      sendJson({ type: 'error', code: 'INVALID_MESSAGE', message: 'Binary messages are not supported.' });
      return;
    }

    let rawPayload: unknown;
    try {
      rawPayload = JSON.parse(data.toString());
    } catch {
      sendJson({ type: 'error', code: 'INVALID_MESSAGE', message: 'WebSocket messages must be valid JSON.' });
      return;
    }

    const parsed = clientMessageSchema.safeParse(rawPayload);
    if (!parsed.success) {
      sendJson({ type: 'error', code: 'INVALID_MESSAGE', message: 'The chat socket message is invalid.' });
      return;
    }

    if (parsed.data.type === 'ping') {
      sendJson({ type: 'pong', timestamp: Date.now() });
    } else if (parsed.data.type === 'cancel') {
      if (activeRequestId === parsed.data.requestId) upstreamController?.abort();
    } else {
      void streamQuery(parsed.data);
    }
  });

  socket.on('pong', () => {
    receivedPong = true;
    if (heartbeatTimeout) {
      clearTimeout(heartbeatTimeout);
      heartbeatTimeout = null;
    }
  });

  socket.on('error', (error) => {
    request.log.warn({ err: error }, 'Chat WebSocket connection failed');
    upstreamController?.abort();
  });

  const heartbeat = setInterval(() => {
    if (socket.readyState !== WebSocket.OPEN) return;
    receivedPong = false;
    socket.ping();
    if (heartbeatTimeout) clearTimeout(heartbeatTimeout);
    heartbeatTimeout = setTimeout(() => {
      if (!receivedPong) socket.terminate();
    }, HEARTBEAT_TIMEOUT_MS);
  }, HEARTBEAT_INTERVAL_MS);

  socket.on('close', () => {
    clearInterval(heartbeat);
    if (heartbeatTimeout) clearTimeout(heartbeatTimeout);
    upstreamController?.abort();
  });
}
