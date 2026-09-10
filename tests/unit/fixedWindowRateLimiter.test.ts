import { describe, expect, test } from 'bun:test';

import { FixedWindowRateLimiter } from '../../services/gateway/src/utils/fixedWindowRateLimiter';

describe('FixedWindowRateLimiter', () => {
  test('enforces a bounded in-memory window when Redis is optional', async () => {
    const limiter = new FixedWindowRateLimiter(undefined, { maxLocalWindows: 10 });
    await limiter.start();

    expect(await limiter.isReady()).toBe(true);
    expect(await limiter.checkRequestRate('tenant', 2)).toMatchObject({
      allowed: true,
      available: true,
      remaining: 1,
    });
    expect(await limiter.checkRequestRate('tenant', 2)).toMatchObject({
      allowed: true,
      available: true,
      remaining: 0,
    });
    expect(await limiter.checkRequestRate('tenant', 2)).toMatchObject({
      allowed: false,
      available: true,
      remaining: 0,
    });
  });

  test('refuses to start without shared storage when Redis is required', async () => {
    const limiter = new FixedWindowRateLimiter(undefined, { redisRequired: true });
    await expect(limiter.start()).rejects.toThrow('REDIS_URL is required');
    expect(await limiter.isReady()).toBe(false);
  });

  test('fails safely when the bounded local fallback is exhausted', async () => {
    const limiter = new FixedWindowRateLimiter(undefined, { maxLocalWindows: 1 });
    await limiter.start();

    expect((await limiter.checkRequestRate('tenant-a')).available).toBe(true);
    expect(await limiter.checkRequestRate('tenant-b')).toMatchObject({
      allowed: false,
      available: false,
      remaining: 0,
    });
  });

  test('rejects an invalid local safety bound', () => {
    expect(() => new FixedWindowRateLimiter(undefined, { maxLocalWindows: 0 })).toThrow(
      'maxLocalWindows must be a positive integer',
    );
  });
});
