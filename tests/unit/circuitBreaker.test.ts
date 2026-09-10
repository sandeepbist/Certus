import { describe, expect, test } from 'bun:test';

import { CircuitBreaker } from '../../services/gateway/src/utils/circuitBreaker';

describe('CircuitBreaker', () => {
  test('opens at the failure threshold and closes after a successful probe', async () => {
    const breaker = new CircuitBreaker('unit-test', {
      failureThreshold: 2,
      cooldownMs: 5,
      windowMs: 1_000,
    });

    const failure = () => Promise.reject(new Error('downstream failed'));

    await expect(breaker.execute(failure)).rejects.toThrow('downstream failed');
    expect(breaker.getState()).toBe('CLOSED');

    await expect(breaker.execute(failure)).rejects.toThrow('downstream failed');
    expect(breaker.getState()).toBe('OPEN');

    await expect(breaker.execute(async () => 'blocked')).rejects.toThrow('Circuit is OPEN');

    await Bun.sleep(10);
    expect(breaker.getState()).toBe('HALF_OPEN');
    await expect(breaker.execute(async () => 'recovered')).resolves.toBe('recovered');
    expect(breaker.getState()).toBe('CLOSED');
  });

  test('reopens when the half-open probe fails', async () => {
    const breaker = new CircuitBreaker('unit-test', {
      failureThreshold: 1,
      cooldownMs: 5,
      windowMs: 1_000,
    });

    await expect(breaker.execute(() => Promise.reject(new Error('first failure')))).rejects.toThrow();
    await Bun.sleep(10);
    expect(breaker.getState()).toBe('HALF_OPEN');

    await expect(breaker.execute(() => Promise.reject(new Error('probe failure')))).rejects.toThrow();
    expect(breaker.getState()).toBe('OPEN');
  });

  test('counts upstream server responses but not caller errors', async () => {
    const breaker = new CircuitBreaker('unit-test', {
      failureThreshold: 2,
      cooldownMs: 5,
      windowMs: 1_000,
    });

    breaker.observeResponse({ status: 429 });
    breaker.observeResponse({ status: 503 });
    expect(breaker.getState()).toBe('CLOSED');
    breaker.observeResponse({ status: 502 });
    expect(breaker.getState()).toBe('OPEN');
  });
});
