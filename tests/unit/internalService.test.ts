import { describe, expect, test } from 'bun:test';
import { EventEmitter } from 'node:events';

import {
  boundedUpstreamTimeout,
  clientDisconnectSignal,
  composeUpstreamSignal,
  internalServiceFetch,
  observedInternalServiceFetch,
} from '../../services/gateway/src/utils/internalService';
import { CircuitBreaker } from '../../services/gateway/src/utils/circuitBreaker';

describe('internal service request lifecycle', () => {
  test('validates named upstream timeout bounds', () => {
    expect(boundedUpstreamTimeout('TEST_TIMEOUT_MS', undefined, 1_000, 5_000)).toBe(1_000);
    expect(boundedUpstreamTimeout('TEST_TIMEOUT_MS', '2500', 1_000, 5_000)).toBe(2_500);
    expect(() => boundedUpstreamTimeout('TEST_TIMEOUT_MS', 'slow', 1_000, 5_000))
      .toThrow('TEST_TIMEOUT_MS');
    expect(() => boundedUpstreamTimeout('TEST_TIMEOUT_MS', '5001', 1_000, 5_000))
      .toThrow('TEST_TIMEOUT_MS');
  });

  test('composes caller cancellation with the operation deadline', async () => {
    const caller = new AbortController();
    const callerFirst = composeUpstreamSignal(1_000, caller.signal);
    caller.abort(new DOMException('caller left', 'AbortError'));
    expect(callerFirst.aborted).toBe(true);
    expect(callerFirst.reason.name).toBe('AbortError');

    const deadlineFirst = composeUpstreamSignal(5);
    await Bun.sleep(15);
    expect(deadlineFirst.aborted).toBe(true);
    expect(deadlineFirst.reason.name).toBe('TimeoutError');
  });

  test('turns the downstream connection lifecycle into an abort signal', () => {
    const incoming = new EventEmitter() as EventEmitter & { aborted: boolean };
    incoming.aborted = false;
    const outgoing = new EventEmitter() as EventEmitter & { destroyed: boolean };
    outgoing.destroyed = false;

    const signal = clientDisconnectSignal(
      { raw: incoming } as never,
      { raw: outgoing } as never,
    );
    expect(signal.aborted).toBe(false);
    outgoing.emit('close');
    expect(signal.aborted).toBe(true);
    expect(incoming.listenerCount('aborted')).toBe(0);
    expect(outgoing.listenerCount('close')).toBe(0);
  });

  test('bounds a real upstream socket and observes server failures', async () => {
    const previousToken = process.env.INTERNAL_SERVICE_TOKEN;
    const previousStreamTimeout = process.env.GATEWAY_UPSTREAM_STREAM_TIMEOUT_MS;
    process.env.INTERNAL_SERVICE_TOKEN = 'unit-test-internal-service-token-0001';
    process.env.GATEWAY_UPSTREAM_STREAM_TIMEOUT_MS = '100';
    let receivedIdentity: { token: string | null; tenant: string | null; user: string | null } | null = null;
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const path = new URL(request.url).pathname;
        receivedIdentity = {
          token: request.headers.get('x-internal-service-token'),
          tenant: request.headers.get('x-certus-tenant-id'),
          user: request.headers.get('x-certus-user-id'),
        };
        if (path === '/unavailable') return new Response('down', { status: 503 });
        await Bun.sleep(1_000);
        return new Response('late');
      },
    });

    try {
      const breaker = new CircuitBreaker('loopback', {
        failureThreshold: 1,
        cooldownMs: 1_000,
        windowMs: 1_000,
      });
      const unavailable = await breaker.execute(() => observedInternalServiceFetch(
        breaker,
        `http://127.0.0.1:${server.port}/unavailable`,
        {},
        { tenantId: 'tenant-1', userId: 'user-1' },
        { profile: 'stream' },
      ));
      expect(unavailable.status).toBe(503);
      expect(breaker.getState()).toBe('OPEN');
      expect(receivedIdentity).toEqual({
        token: 'unit-test-internal-service-token-0001',
        tenant: 'tenant-1',
        user: 'user-1',
      });

      const startedAt = performance.now();
      await expect(internalServiceFetch(
        `http://127.0.0.1:${server.port}/slow`,
        {},
        undefined,
        { profile: 'stream' },
      )).rejects.toThrow();
      expect(performance.now() - startedAt).toBeLessThan(500);
    } finally {
      server.stop(true);
      if (previousToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
      else process.env.INTERNAL_SERVICE_TOKEN = previousToken;
      if (previousStreamTimeout === undefined) {
        delete process.env.GATEWAY_UPSTREAM_STREAM_TIMEOUT_MS;
      } else {
        process.env.GATEWAY_UPSTREAM_STREAM_TIMEOUT_MS = previousStreamTimeout;
      }
    }
  });
});
