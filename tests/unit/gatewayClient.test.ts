import { afterEach, describe, expect, test } from 'bun:test';

import {
  gatewayFetch,
  gatewayRequestSignal,
} from '../../services/web/src/lib/gateway-client';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe('browser-to-Gateway request lifecycle', () => {
  test('normalizes the path and always attaches a same-origin deadline', async () => {
    let capturedInput = '';
    let capturedInit: RequestInit | undefined;
    globalThis.fetch = (async (input, init) => {
      capturedInput = String(input);
      capturedInit = init;
      return Response.json({ ok: true });
    }) as typeof fetch;

    const response = await gatewayFetch('tasks');

    expect(response.ok).toBeTrue();
    expect(capturedInput).toBe('/api/gateway/tasks');
    expect(capturedInit?.credentials).toBe('same-origin');
    expect(capturedInit?.signal).toBeInstanceOf(AbortSignal);
  });

  test('aborts a genuinely stalled browser request at its owned deadline', async () => {
    globalThis.fetch = ((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    })) as typeof fetch;

    const started = performance.now();
    await expect(gatewayFetch('/stall', {}, { timeoutMs: 100 })).rejects.toBeInstanceOf(DOMException);
    const elapsed = performance.now() - started;

    expect(elapsed).toBeGreaterThanOrEqual(80);
    expect(elapsed).toBeLessThan(500);
  });

  test('composes caller cancellation with the longer operation profile', () => {
    const caller = new AbortController();
    const signal = gatewayRequestSignal('processing', caller.signal);
    const reason = new Error('component unmounted');

    caller.abort(reason);

    expect(signal.aborted).toBeTrue();
    expect(signal.reason).toBe(reason);
  });

  test('rejects unsafe deadline overrides before opening a request', () => {
    expect(() => gatewayRequestSignal('interactive', undefined, 99)).toThrow();
    expect(() => gatewayRequestSignal('interactive', undefined, 900_001)).toThrow();
    expect(() => gatewayRequestSignal('interactive', undefined, 1_000.5)).toThrow();
  });
});
