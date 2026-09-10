import { Redis } from 'ioredis';

export type RateLimitResult = {
  allowed: boolean;
  available: boolean;
  remaining: number;
  reset: number;
};

type LocalWindow = {
  count: number;
  reset: number;
};

type RateLimiterOptions = {
  redisRequired?: boolean;
  maxLocalWindows?: number;
};

const incrementWindowScript = `
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
`;

export class FixedWindowRateLimiter {
  private redis: Redis | null = null;
  private readonly localWindows = new Map<string, LocalWindow>();
  private readonly redisRequired: boolean;
  private readonly maxLocalWindows: number;

  constructor(
    private readonly redisUrl?: string,
    options: RateLimiterOptions = {},
  ) {
    this.redisRequired = options.redisRequired ?? false;
    this.maxLocalWindows = options.maxLocalWindows ?? 10_000;
    if (!Number.isSafeInteger(this.maxLocalWindows) || this.maxLocalWindows < 1) {
      throw new Error('maxLocalWindows must be a positive integer.');
    }
  }

  async start(): Promise<void> {
    if (!this.redisUrl) {
      if (this.redisRequired) {
        throw new Error('REDIS_URL is required for production rate limiting.');
      }
      return;
    }

    const redis = new Redis(this.redisUrl, {
      maxRetriesPerRequest: 1,
      enableOfflineQueue: false,
      lazyConnect: true,
    });
    redis.on('error', () => {
      // Request checks surface storage outages through their result contract.
    });
    try {
      await redis.connect();
      await redis.ping();
      this.redis = redis;
    } catch (error) {
      redis.disconnect(false);
      if (this.redisRequired) throw error;
    }
  }

  async stop(): Promise<void> {
    if (!this.redis) return;
    this.redis.disconnect(false);
    this.redis = null;
  }

  async isReady(): Promise<boolean> {
    if (!this.redis) return !this.redisRequired;
    try {
      return await this.redis.ping() === 'PONG';
    } catch {
      return false;
    }
  }

  private checkLocalWindow(
    windowKey: string,
    maxPerMinute: number,
    now: number,
    reset: number,
  ): RateLimitResult {
    for (const [key, window] of this.localWindows) {
      if (window.reset <= now) this.localWindows.delete(key);
    }

    const existing = this.localWindows.get(windowKey);
    if (!existing && this.localWindows.size >= this.maxLocalWindows) {
      return { allowed: false, available: false, remaining: 0, reset };
    }

    const current = (existing?.count ?? 0) + 1;
    this.localWindows.set(windowKey, { count: current, reset });
    return {
      allowed: current <= maxPerMinute,
      available: true,
      remaining: Math.max(0, maxPerMinute - current),
      reset,
    };
  }

  async checkRequestRate(tenantId: string, maxPerMinute = 100): Promise<RateLimitResult> {
    if (!Number.isInteger(maxPerMinute) || maxPerMinute < 1) {
      throw new Error('maxPerMinute must be a positive integer.');
    }

    const now = Math.floor(Date.now() / 1_000);
    const minute = Math.floor(now / 60);
    const windowKey = `ratelimit:${tenantId}:${minute}`;
    const reset = (minute + 1) * 60;

    if (this.redis) {
      try {
        const current = Number(await this.redis.eval(
          incrementWindowScript,
          1,
          windowKey,
          65,
        ));
        if (!Number.isSafeInteger(current) || current < 1) {
          throw new Error('Redis returned an invalid rate-limit counter.');
        }
        return {
          allowed: current <= maxPerMinute,
          available: true,
          remaining: Math.max(0, maxPerMinute - current),
          reset,
        };
      } catch {
        if (this.redisRequired) {
          return { allowed: false, available: false, remaining: 0, reset };
        }
      }
    }

    return this.checkLocalWindow(windowKey, maxPerMinute, now, reset);
  }
}
