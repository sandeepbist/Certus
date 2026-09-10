import type { FastifyReply, FastifyRequest } from 'fastify';

import { AuthUserContext } from '../types/index.js';
import type { CircuitBreaker } from './circuitBreaker.js';

export type InternalServiceProfile = 'interactive' | 'processing' | 'stream';

type InternalServiceOptions = {
  profile?: InternalServiceProfile;
};

const timeoutProfiles: Record<
  InternalServiceProfile,
  { environmentName: string; defaultMs: number; maximumMs: number }
> = {
  interactive: {
    environmentName: 'GATEWAY_UPSTREAM_INTERACTIVE_TIMEOUT_MS',
    defaultMs: 15_000,
    maximumMs: 120_000,
  },
  processing: {
    environmentName: 'GATEWAY_UPSTREAM_PROCESSING_TIMEOUT_MS',
    defaultMs: 180_000,
    maximumMs: 900_000,
  },
  stream: {
    environmentName: 'GATEWAY_UPSTREAM_STREAM_TIMEOUT_MS',
    defaultMs: 600_000,
    maximumMs: 3_600_000,
  },
};

export function boundedUpstreamTimeout(
  environmentName: string,
  rawValue: string | undefined,
  defaultMs: number,
  maximumMs: number,
): number {
  const value = rawValue === undefined || rawValue.trim() === ''
    ? defaultMs
    : Number(rawValue);
  if (!Number.isInteger(value) || value < 100 || value > maximumMs) {
    throw new Error(`${environmentName} must be an integer between 100 and ${maximumMs} milliseconds.`);
  }
  return value;
}

export function configuredUpstreamTimeout(profile: InternalServiceProfile): number {
  const definition = timeoutProfiles[profile];
  return boundedUpstreamTimeout(
    definition.environmentName,
    process.env[definition.environmentName],
    definition.defaultMs,
    definition.maximumMs,
  );
}

export function composeUpstreamSignal(timeoutMs: number, callerSignal?: AbortSignal): AbortSignal {
  const deadline = AbortSignal.timeout(timeoutMs);
  return callerSignal ? AbortSignal.any([callerSignal, deadline]) : deadline;
}

export function clientDisconnectSignal(
  request: Pick<FastifyRequest, 'raw'>,
  reply: Pick<FastifyReply, 'raw'>,
): AbortSignal {
  const controller = new AbortController();
  const cleanup = () => {
    request.raw.off('aborted', abort);
    reply.raw.off('close', abort);
  };
  const abort = () => {
    cleanup();
    if (!controller.signal.aborted) {
      controller.abort(new DOMException('Client disconnected', 'AbortError'));
    }
  };

  if (request.raw.aborted || reply.raw.destroyed) {
    abort();
  } else {
    request.raw.once('aborted', abort);
    reply.raw.once('close', abort);
  }
  return controller.signal;
}

export function assertInternalServiceConfiguration() {
  const token = process.env.INTERNAL_SERVICE_TOKEN?.trim();
  if (!token || token.length < 32) {
    throw new Error('INTERNAL_SERVICE_TOKEN must be configured with at least 32 characters.');
  }
  for (const profile of Object.keys(timeoutProfiles) as InternalServiceProfile[]) {
    configuredUpstreamTimeout(profile);
  }
}

export async function internalServiceFetch(
  input: string | URL,
  init: RequestInit = {},
  context?: Pick<AuthUserContext, 'tenantId' | 'userId'>,
  options: InternalServiceOptions = {},
): Promise<Response> {
  const token = process.env.INTERNAL_SERVICE_TOKEN?.trim();
  if (!token) {
    throw new Error('INTERNAL_SERVICE_TOKEN is not configured.');
  }

  const headers = new Headers(init.headers);
  headers.set('x-internal-service-token', token);

  if (context) {
    headers.set('x-certus-tenant-id', context.tenantId);
    headers.set('x-certus-user-id', context.userId);
  }

  const profile = options.profile ?? 'interactive';
  const signal = composeUpstreamSignal(
    configuredUpstreamTimeout(profile),
    init.signal ?? undefined,
  );
  return fetch(input, { ...init, headers, signal });
}

/**
 * Use inside the matching breaker.execute() action. Network/deadline failures
 * are recorded by execute(); this adds status-based failure observation
 * without hiding the downstream response body from the route.
 */
export async function observedInternalServiceFetch(
  breaker: CircuitBreaker,
  input: string | URL,
  init: RequestInit = {},
  context?: Pick<AuthUserContext, 'tenantId' | 'userId'>,
  options: InternalServiceOptions = {},
): Promise<Response> {
  const response = await internalServiceFetch(input, init, context, options);
  breaker.observeResponse(response);
  return response;
}
