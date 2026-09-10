import type { FastifyBaseLogger } from 'fastify';
import { Redis } from 'ioredis';
import { WebSocket } from 'ws';
import { z } from 'zod';

const GATEWAY_STREAM_KEY = 'realtime:gateway';
const BLOCK_TIMEOUT_MS = 2_000;
const MAX_BATCH_SIZE = 100;
const MAX_DEDUPLICATION_IDS = 5_000;
const MAX_SOCKET_BUFFERED_BYTES = 1024 * 1024;
const REDIS_ERROR_LOG_INTERVAL_MS = 5_000;

export const realtimeChannelSchema = z.string().regex(
  /^(?:trace|document):[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
);

export const realtimeStreamEventSchema = z.object({
  event_id: z.string().uuid(),
  tenant_id: z.string().min(1).max(255),
  user_id: z.string().min(1).max(255),
  channel: realtimeChannelSchema,
  event_type: z.enum(['trace.event', 'trace.status', 'document.status']),
  data: z.record(z.string(), z.unknown()),
  created_at: z.number().int().nonnegative(),
});

export type RealtimeStreamEvent = z.infer<typeof realtimeStreamEventSchema>;

export type RealtimeSubscription = {
  socket: WebSocket;
  tenantId: string;
  userId: string;
  channels: Set<string>;
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

export function parseRealtimeStreamEvent(fields: string[]): RealtimeStreamEvent | null {
  const record = fieldsToRecord(fields);
  if (!record.event_json) return null;
  try {
    const parsedJson: unknown = JSON.parse(record.event_json);
    const parsed = realtimeStreamEventSchema.safeParse(parsedJson);
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

function sendEvent(subscription: RealtimeSubscription, event: RealtimeStreamEvent) {
  if (
    subscription.socket.readyState !== WebSocket.OPEN
    || !subscription.channels.has(event.channel)
  ) return;
  if (subscription.socket.bufferedAmount > MAX_SOCKET_BUFFERED_BYTES) {
    // Product-state clients recover from REST snapshots after reconnecting, so
    // terminating a slow peer is safer than growing an unbounded send buffer.
    subscription.socket.terminate();
    return;
  }
  try {
    subscription.socket.send(JSON.stringify({
      type: event.event_type,
      event_id: event.event_id,
      channel: event.channel,
      data: event.data,
    }));
  } catch {
    subscription.socket.terminate();
  }
}

export class RealtimeBroker {
  private readonly redis: Redis;
  private readonly subscriptions = new Map<string, Set<RealtimeSubscription>>();
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
      this.logger.error({ err: error }, 'Realtime Redis connection unavailable');
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

  subscribe(socket: WebSocket, tenantId: string, userId: string): RealtimeSubscription {
    const key = subscriptionKey(tenantId, userId);
    const subscription: RealtimeSubscription = {
      socket,
      tenantId,
      userId,
      channels: new Set(),
    };
    const existing = this.subscriptions.get(key) || new Set<RealtimeSubscription>();
    existing.add(subscription);
    this.subscriptions.set(key, existing);
    return subscription;
  }

  unsubscribe(subscription: RealtimeSubscription) {
    const key = subscriptionKey(subscription.tenantId, subscription.userId);
    const existing = this.subscriptions.get(key);
    if (!existing) return;
    existing.delete(subscription);
    if (existing.size === 0) this.subscriptions.delete(key);
  }

  private broadcast(event: RealtimeStreamEvent) {
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
    for (const subscription of subscribers) sendEvent(subscription, event);
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
            const event = parseRealtimeStreamEvent(fields);
            if (event) {
              this.broadcast(event);
            } else {
              this.logger.warn({ streamId }, 'Ignored malformed realtime stream event');
            }
          }
        }
      } catch (error) {
        if (this.stopped) return;
        this.logger.warn({ err: error }, 'Realtime Redis stream read failed; retrying');
        await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
        retryDelayMs = Math.min(retryDelayMs * 2, 30_000);
      }
    }
  }
}
