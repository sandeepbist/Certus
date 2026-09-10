import { CircuitBreakerConfig, CircuitBreakerState } from '../types/index.js';

export class CircuitBreaker {
  private state: CircuitBreakerState = 'CLOSED';
  private failures: number[] = [];
  private lastOpenedAt = 0;
  private halfOpenProbeInFlight = false;

  constructor(
    public readonly name: string,
    private readonly config: CircuitBreakerConfig = {
      failureThreshold: 5,
      cooldownMs: 30000,
      windowMs: 60000,
    }
  ) {}

  getState(): CircuitBreakerState {
    const now = Date.now();
    if (this.state === 'OPEN') {
      if (now - this.lastOpenedAt >= this.config.cooldownMs) {
        this.state = 'HALF_OPEN';
        this.halfOpenProbeInFlight = false;
      }
    }
    return this.state;
  }

  async execute<T>(action: () => Promise<T>): Promise<T> {
    const currentState = this.getState();

    if (currentState === 'OPEN') {
      throw new Error(`[CircuitBreaker:${this.name}] Circuit is OPEN. Service unavailable.`);
    }

    if (currentState === 'HALF_OPEN') {
      if (this.halfOpenProbeInFlight) {
        throw new Error(`[CircuitBreaker:${this.name}] Probe in flight. Service recovering.`);
      }
      this.halfOpenProbeInFlight = true;
    }

    try {
      const result = await action();
      this.recordSuccess();
      return result;
    } catch (err) {
      this.recordFailure();
      throw err;
    }
  }

  observeResponse(response: Pick<Response, 'status'>): void {
    if (response.status >= 500) this.recordFailure();
  }

  private recordSuccess(): void {
    if (this.state === 'HALF_OPEN') {
      this.state = 'CLOSED';
      this.failures = [];
      this.halfOpenProbeInFlight = false;
    }
  }

  private recordFailure(): void {
    const now = Date.now();
    if (this.state === 'HALF_OPEN') {
      this.state = 'OPEN';
      this.lastOpenedAt = now;
      this.halfOpenProbeInFlight = false;
      return;
    }

    // Prune stale failures outside window
    this.failures = this.failures.filter((timestamp) => now - timestamp < this.config.windowMs);
    this.failures.push(now);

    if (this.failures.length >= this.config.failureThreshold) {
      this.state = 'OPEN';
      this.lastOpenedAt = now;
    }
  }
}

// Breakers registry
export const ingestionBreaker = new CircuitBreaker('ingestion_service');
export const orchestrationBreaker = new CircuitBreaker('orchestration_service');
export const mcpBreaker = new CircuitBreaker('mcp_service');
