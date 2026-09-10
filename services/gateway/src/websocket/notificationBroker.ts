import type { FastifyBaseLogger } from 'fastify';
import { Redis } from 'ioredis';
import { WebSocket } from 'ws';
import { z } from 'zod';

const GATEWAY_STREAM_KEY = 'notifications:gateway';
const BLOCK_TIMEOUT_MS = 2_000;
const MAX_BATCH_SIZE = 100;
const MAX_PENDING_PER_SOCKET = 100;
const MAX_DEDUPLICATION_IDS = 5_000;
const MAX_SOCKET_BUFFERED_BYTES = 1024 * 1024;
const REDIS_ERROR_LOG_INTERVAL_MS = 5_000;

const notificationSchema = z.object({
  id: z.string().uuid(),
  type: z.string().min(1).max(50),
  title: z.string().min(1).max(255),
  body: z.string().nullable(),
  metadata: z.record(z.string(), z.unknown()),
  action_url: z.string().max(500).nullable(),
  is_read: z.boolean(),
  created_at: z.string().min(1),
});

export const notificationStreamEventSchema = z.object({
  event_id: z.string().uuid(),
  notification_id: z.string().uuid(),
  tenant_id: z.string().min(1).max(255),
  user_id: z.string().min(1).max(255),
  event_type: z.enum(['created', 'read_state_changed', 'deleted']),
  notification: notificationSchema,
  created_at: z.number().int().nonnegative(),
});

export type NotificationStreamEvent = z.infer<typeof notificationStreamEventSchema>;

type Subscription = {
  socket: WebSocket;
  tenantId: string;
  userId: string;
  ready: boolean;
  pending: NotificationStreamEvent[];
};

function subscriptionKey(tenantId: string, userId: string) {
  return `${tenantId}\u0000${userId}`;
}

function fieldsToRecord(fields: string[]): Record<string, string> {
  const record: Record<string, string> = {};
  for (let index = 0; index < fields.length; index += 2) {
    const key = fields[index];
    const value = fields[index + 1];
    if (key !== undefined && value !== undefined) record[key] = value;
  }
  return record;
}

export function parseNotificationStreamEvent(fields: string[]): NotificationStreamEvent | null {
  const record = fieldsToRecord(fields);
  if (!record.event_json) return null;
  try {
    const parsedJson: unknown = JSON.parse(record.event_json);
    const parsed = notificationStreamEventSchema.safeParse(parsedJson);
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

function sendNotification(socket: WebSocket, event: NotificationStreamEvent) {
  if (socket.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
    socket.terminate();
    return;
  }
  try {
    socket.send(JSON.stringify({
      type: 'notification',
      event_id: event.event_id,
      event: event.event_type,
      data: event.notification,
    }));
  } catch {
    socket.terminate();
  }
}

export class NotificationBroker {
  private readonly redis: Redis;
  private readonly subscriptions = new Map<string, Set<Subscription>>();
  private readonly seenEventIds = new Set<string>();
  private listenPromise: Promise<void> | null = null;
  private stopped = true;
  private lastStreamId = '$';
  private lastRedisErrorLogAt = 0;

  constructor(redisUrl: string, private readonly logger: FastifyBaseLogger) {
    this.redis = new Redis(redisUrl, {
      lazyConnect: true,
      maxRetriesPerRequest: null,
      enableOfflineQueue: true,
    });
    this.redis.on('error', (error) => {
      const now = Date.now();
      if (now - this.lastRedisErrorLogAt < REDIS_ERROR_LOG_INTERVAL_MS) return;
      this.lastRedisErrorLogAt = now;
      this.logger.error({ err: error }, 'Notification Redis connection unavailable');
    });
  }

  start() {
    if (this.listenPromise) return;
    this.stopped = false;
    this.lastStreamId = '$';
    this.listenPromise = this.listen().finally(() => {
      this.listenPromise = null;
    });
  }

  async stop() {
    this.stopped = true;
    this.redis.disconnect();
    await this.listenPromise?.catch(() => undefined);
    this.subscriptions.clear();
  }

  subscribe(socket: WebSocket, tenantId: string, userId: string): Subscription {
    const key = subscriptionKey(tenantId, userId);
    const subscription: Subscription = {
      socket,
      tenantId,
      userId,
      ready: false,
      pending: [],
    };
    const existing = this.subscriptions.get(key) || new Set<Subscription>();
    existing.add(subscription);
    this.subscriptions.set(key, existing);
    return subscription;
  }

  activate(subscription: Subscription) {
    subscription.ready = true;
    const pending = subscription.pending.splice(0);
    for (const event of pending) sendNotification(subscription.socket, event);
  }

  unsubscribe(subscription: Subscription) {
    const key = subscriptionKey(subscription.tenantId, subscription.userId);
    const existing = this.subscriptions.get(key);
    if (!existing) return;
    existing.delete(subscription);
    if (existing.size === 0) this.subscriptions.delete(key);
  }

  private broadcast(event: NotificationStreamEvent) {
    if (this.seenEventIds.has(event.event_id)) return;
    this.seenEventIds.add(event.event_id);
    if (this.seenEventIds.size > MAX_DEDUPLICATION_IDS) {
      const oldestEventId = this.seenEventIds.values().next().value;
      if (oldestEventId) this.seenEventIds.delete(oldestEventId);
    }

    const subscribers = this.subscriptions.get(
      subscriptionKey(event.tenant_id, event.user_id),
    );
    if (!subscribers) return;
    for (const subscription of subscribers) {
      if (subscription.ready) {
        sendNotification(subscription.socket, event);
        continue;
      }
      if (subscription.pending.length >= MAX_PENDING_PER_SOCKET) {
        subscription.pending.shift();
      }
      subscription.pending.push(event);
    }
  }

  private async connect() {
    if (this.redis.status === 'wait' || this.redis.status === 'end') {
      await this.redis.connect();
    }
  }

  private async listen() {
    let retryDelayMs = 1_000;
    while (!this.stopped) {
      try {
        await this.connect();
        const response = await this.redis.xread(
          'COUNT',
          MAX_BATCH_SIZE,
          'BLOCK',
          BLOCK_TIMEOUT_MS,
          'STREAMS',
          GATEWAY_STREAM_KEY,
          this.lastStreamId,
        ) as [string, [string, string[]][]][] | null;
        retryDelayMs = 1_000;
        if (!response) continue;
        for (const [, messages] of response) {
          for (const [streamId, fields] of messages) {
            this.lastStreamId = streamId;
            const event = parseNotificationStreamEvent(fields);
            if (event) {
              this.broadcast(event);
            } else {
              this.logger.warn({ streamId }, 'Ignored malformed notification stream event');
            }
          }
        }
      } catch (error) {
        if (this.stopped) return;
        this.logger.warn({ err: error }, 'Notification Redis stream read failed; retrying');
        await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
        retryDelayMs = Math.min(retryDelayMs * 2, 30_000);
      }
    }
  }
}
