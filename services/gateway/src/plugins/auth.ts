import { FastifyPluginAsync, FastifyReply, FastifyRequest } from 'fastify';
import fp from 'fastify-plugin';
import { z } from 'zod';

import { AuthUserContext } from '../types/index.js';
import { apiKeyAuthorizesMethod } from '../utils/apiKeyScopes.js';

declare module 'fastify' {
  interface FastifyRequest {
    user?: AuthUserContext;
  }
}

const authContextSchema = z.object({
  userId: z.string().min(1),
  tenantId: z.string().min(1),
  roles: z.array(z.string().min(1)).min(1),
  tokenBudget: z.number().int().nonnegative(),
  authType: z.enum(['session', 'api_key']),
  scopes: z.array(z.enum(['read', 'write', 'admin'])).min(1),
  expiresAt: z.string().datetime(),
});

const publicPaths = new Set(['/health', '/health/ready', '/favicon.ico']);
const authServiceTimeoutMs = Number(
  process.env.AUTH_SERVICE_TIMEOUT_MS || (process.env.NODE_ENV === 'production' ? '5000' : '15000'),
);

if (!Number.isFinite(authServiceTimeoutMs) || authServiceTimeoutMs <= 0) {
  throw new Error('AUTH_SERVICE_TIMEOUT_MS must be a positive number.');
}

function authFailure(reply: FastifyReply, status: number) {
  if (status === 401) {
    return reply.status(401).send({
      error: 'Unauthorized',
      message: 'The session or API key is invalid or expired.',
    });
  }
  if (status === 403) {
    return reply.status(403).send({
      error: 'WorkspaceRequired',
      message: 'Select or create a workspace first.',
    });
  }
  return reply.status(503).send({
    error: 'AuthenticationUnavailable',
    message: 'Authentication could not be verified. Please try again.',
  });
}

async function requestAuthContext(
  request: FastifyRequest,
  authServiceUrl: string,
): Promise<Response | null> {
  const authorization = request.headers.authorization;
  if (authorization) {
    const match = /^Bearer\s+([^\s]+)$/i.exec(authorization);
    if (!match) return null;
    const internalToken = process.env.INTERNAL_SERVICE_TOKEN?.trim();
    if (!internalToken) throw new Error('INTERNAL_SERVICE_TOKEN is not configured.');
    return fetch(`${authServiceUrl}/api/api-key-context`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-internal-service-token': internalToken,
      },
      body: JSON.stringify({ key: match[1] }),
      signal: AbortSignal.timeout(authServiceTimeoutMs),
    });
  }

  const sessionCookie = request.headers.cookie;
  if (!sessionCookie) return null;
  return fetch(`${authServiceUrl}/api/session-context`, {
    method: 'GET',
    headers: {
      cookie: sessionCookie,
      ...(request.headers['user-agent']
        ? { 'user-agent': request.headers['user-agent'] }
        : {}),
    },
    signal: AbortSignal.timeout(authServiceTimeoutMs),
  });
}

const authPlugin: FastifyPluginAsync = async (fastify) => {
  const authServiceUrl = (
    process.env.AUTH_SERVICE_URL
    || process.env.BETTER_AUTH_URL
    || 'http://localhost:3000'
  ).replace(/\/$/, '');

  fastify.decorateRequest('user', undefined);

  fastify.addHook('preHandler', async (request, reply) => {
    const requestPath = request.url.split('?', 1)[0];
    if (publicPaths.has(requestPath)) return;

    let authResponse: Response | null;
    try {
      authResponse = await requestAuthContext(request, authServiceUrl);
    } catch (error) {
      request.log.error({ err: error }, 'Authentication service unavailable');
      return authFailure(reply, 503);
    }
    if (!authResponse) return authFailure(reply, 401);
    if (!authResponse.ok) {
      if (authResponse.status !== 401 && authResponse.status !== 403) {
        request.log.error(
          { authStatus: authResponse.status },
          'Authentication service returned an unexpected response',
        );
      }
      return authFailure(reply, authResponse.status);
    }

    let authPayload: unknown;
    try {
      authPayload = await authResponse.json();
    } catch (error) {
      request.log.error({ err: error }, 'Authentication service returned malformed JSON');
      return authFailure(reply, 503);
    }

    const parsed = authContextSchema.safeParse(authPayload);
    if (!parsed.success || new Date(parsed.data.expiresAt).getTime() <= Date.now()) {
      request.log.warn('Authentication service returned an invalid auth context');
      return authFailure(reply, 401);
    }
    if (
      parsed.data.authType === 'api_key'
      && !apiKeyAuthorizesMethod(parsed.data.scopes, request.method)
    ) {
      return reply.status(403).send({
        error: 'InsufficientScope',
        message: `This API key cannot perform ${request.method} requests.`,
      });
    }

    request.user = {
      userId: parsed.data.userId,
      tenantId: parsed.data.tenantId,
      roles: parsed.data.roles,
      tokenBudget: parsed.data.tokenBudget,
      authType: parsed.data.authType,
      scopes: parsed.data.scopes,
    };
  });
};

export default fp(authPlugin, { name: 'certus-auth' });
