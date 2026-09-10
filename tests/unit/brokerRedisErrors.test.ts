import { describe, expect, test } from 'bun:test';
import type { FastifyBaseLogger } from 'fastify';

import { NotificationBroker } from '../../services/gateway/src/websocket/notificationBroker';
import { RealtimeBroker } from '../../services/gateway/src/websocket/realtimeBroker';

type ErrorEmitter = {
  emit(event: 'error', error: Error): boolean;
};

function loggerProbe() {
  const messages: string[] = [];
  const logger = {
    error: (_context: unknown, message: string) => messages.push(message),
  } as unknown as FastifyBaseLogger;
  return { logger, messages };
}

describe('gateway broker Redis errors', () => {
  test('handles and rate-bounds notification Redis error events', async () => {
    const { logger, messages } = loggerProbe();
    const broker = new NotificationBroker('redis://127.0.0.1:1', logger);
    const redis = (broker as unknown as { redis: ErrorEmitter }).redis;

    redis.emit('error', new Error('unavailable'));
    redis.emit('error', new Error('still unavailable'));

    expect(messages).toEqual(['Notification Redis connection unavailable']);
    await broker.stop();
  });

  test('handles and rate-bounds realtime Redis error events', async () => {
    const { logger, messages } = loggerProbe();
    const broker = new RealtimeBroker('redis://127.0.0.1:1', logger);
    const redis = (broker as unknown as { redis: ErrorEmitter }).redis;

    redis.emit('error', new Error('unavailable'));
    redis.emit('error', new Error('still unavailable'));

    expect(messages).toEqual(['Realtime Redis connection unavailable']);
    await broker.stop();
  });
});
