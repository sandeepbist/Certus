import type { FastifyRequest } from 'fastify';
import { WebSocket } from 'ws';
import { z } from 'zod';

import { requireAuthContext } from '../utils/authContext.js';
import {
  RealtimeBroker,
  realtimeChannelSchema,
} from './realtimeBroker.js';

const HEARTBEAT_INTERVAL_MS = 30_000;
const HEARTBEAT_TIMEOUT_MS = 10_000;
const MAX_CHANNELS_PER_SOCKET = 100;

const clientMessageSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('ping') }).strict(),
  z.object({
    type: z.literal('subscribe'),
    channels: z.array(realtimeChannelSchema).min(1).max(MAX_CHANNELS_PER_SOCKET),
  }).strict(),
  z.object({
    type: z.literal('unsubscribe'),
    channels: z.array(realtimeChannelSchema).min(1).max(MAX_CHANNELS_PER_SOCKET),
  }).strict(),
]);

export function handleRealtimeConnection(
  socket: WebSocket,
  request: FastifyRequest,
  broker: RealtimeBroker,
) {
  const { tenantId, userId } = requireAuthContext(request);
  const subscription = broker.subscribe(socket, tenantId, userId);
  let receivedPong = true;
  let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null;

  const sendJson = (payload: unknown) => {
    if (socket.readyState !== WebSocket.OPEN) return;
    try {
      socket.send(JSON.stringify(payload));
    } catch (error) {
      request.log.warn({ err: error }, 'Realtime WebSocket send failed');
      socket.terminate();
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
      sendJson({ type: 'error', code: 'INVALID_MESSAGE', message: 'The realtime socket message is invalid.' });
      return;
    }
    if (parsed.data.type === 'ping') {
      sendJson({ type: 'pong', timestamp: Date.now() });
      return;
    }
    if (parsed.data.type === 'subscribe') {
      const combined = new Set([...subscription.channels, ...parsed.data.channels]);
      if (combined.size > MAX_CHANNELS_PER_SOCKET) {
        sendJson({
          type: 'error',
          code: 'CHANNEL_LIMIT_EXCEEDED',
          message: `A socket can subscribe to at most ${MAX_CHANNELS_PER_SOCKET} channels.`,
        });
        return;
      }
      subscription.channels = combined;
    } else {
      for (const channel of parsed.data.channels) subscription.channels.delete(channel);
    }
    sendJson({ type: 'subscribed', channels: [...subscription.channels].sort() });
  });

  socket.on('pong', () => {
    receivedPong = true;
    if (heartbeatTimeout) {
      clearTimeout(heartbeatTimeout);
      heartbeatTimeout = null;
    }
  });

  socket.on('error', (error) => {
    request.log.warn({ err: error }, 'Realtime WebSocket connection failed');
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
    broker.unsubscribe(subscription);
  });
}
