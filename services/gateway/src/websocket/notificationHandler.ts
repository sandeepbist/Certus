import type { FastifyRequest } from 'fastify';
import { WebSocket } from 'ws';
import { z } from 'zod';

import { requireAuthContext } from '../utils/authContext.js';
import { internalServiceFetch } from '../utils/internalService.js';
import { NotificationBroker } from './notificationBroker.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';
const HEARTBEAT_INTERVAL_MS = 30_000;
const HEARTBEAT_TIMEOUT_MS = 10_000;

const notificationSummarySchema = z.object({
  notifications: z.array(z.object({
    id: z.string().uuid(),
    type: z.string(),
    title: z.string(),
    body: z.string().nullable(),
    metadata: z.record(z.string(), z.unknown()),
    action_url: z.string().nullable(),
    is_read: z.boolean(),
    created_at: z.string(),
  })),
  unread_count: z.number().int().nonnegative(),
});

function sendJson(socket: WebSocket, payload: unknown) {
  if (socket.readyState !== WebSocket.OPEN) return;
  try {
    socket.send(JSON.stringify(payload));
  } catch {
    socket.terminate();
  }
}

export function handleNotificationConnection(
  socket: WebSocket,
  request: FastifyRequest,
  broker: NotificationBroker,
) {
  const { tenantId, userId } = requireAuthContext(request);
  const subscription = broker.subscribe(socket, tenantId, userId);
  const initialSyncController = new AbortController();
  let receivedPong = true;
  let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null;

  socket.on('message', (data) => {
    try {
      const payload: unknown = JSON.parse(data.toString());
      if (
        typeof payload === 'object'
        && payload !== null
        && 'type' in payload
        && payload.type === 'ping'
      ) {
        sendJson(socket, { type: 'pong', timestamp: Date.now() });
        return;
      }
      sendJson(socket, { type: 'error', message: 'Unsupported notification socket message.' });
    } catch {
      sendJson(socket, { type: 'error', message: 'WebSocket messages must be valid JSON.' });
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
    request.log.warn({ err: error }, 'Notification WebSocket connection failed');
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
    initialSyncController.abort(new DOMException('Notification socket closed', 'AbortError'));
    broker.unsubscribe(subscription);
  });

  void (async () => {
    try {
      const response = await internalServiceFetch(
        `${ORCHESTRATION_SERVICE_URL}/notifications?status=unread&limit=5`,
        { signal: initialSyncController.signal },
        { tenantId, userId },
      );
      if (!response.ok) throw new Error(`Notification service returned ${response.status}`);
      const summary = notificationSummarySchema.parse(await response.json());
      sendJson(socket, { type: 'notifications.sync', data: summary });
    } catch (error) {
      request.log.warn({ err: error }, 'Initial WebSocket notification sync failed');
      sendJson(socket, {
        type: 'notifications.sync_error',
        message: 'Notifications will resynchronize automatically.',
      });
    } finally {
      broker.activate(subscription);
    }
  })();
}
