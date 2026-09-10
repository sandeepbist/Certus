import { describe, expect, test } from 'bun:test';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import type { FastifyReply, FastifyRequest } from 'fastify';
import Fastify from 'fastify';

import { toolRoutes } from '../../services/gateway/src/routes/tools';
import { sendServiceUnavailable } from '../../services/gateway/src/utils/serviceUnavailable';

describe('Gateway internal-service error boundary', () => {
  test('logs the original failure but returns only the stable public message', () => {
    const internalError = new Error(
      'connect ECONNREFUSED http://internal-user:secret@orchestration:8002',
    );
    const logRecords: Array<{ record: unknown; message: string }> = [];
    const request = {
      log: {
        error: (record: unknown, message: string) => logRecords.push({ record, message }),
      },
    } as unknown as FastifyRequest;
    let status = 0;
    let body: unknown;
    const reply = {
      status: (value: number) => {
        status = value;
        return reply;
      },
      send: (value: unknown) => {
        body = value;
        return value;
      },
    } as unknown as FastifyReply;

    sendServiceUnavailable(
      request,
      reply,
      'orchestration',
      'Workspace search is temporarily unavailable.',
      internalError,
    );

    expect(status).toBe(503);
    expect(body).toEqual({
      error: 'Service Unavailable',
      message: 'Workspace search is temporarily unavailable.',
    });
    expect(JSON.stringify(body)).not.toContain('secret');
    expect(logRecords).toEqual([{
      record: { err: internalError, service: 'orchestration' },
      message: 'Internal service request failed',
    }]);
  });

  test('no HTTP route projects a caught Error.message into a public response', () => {
    const routesDirectory = path.resolve(import.meta.dir, '../../services/gateway/src/routes');
    const routeSources = readdirSync(routesDirectory)
      .filter((fileName) => fileName.endsWith('.ts'))
      .map((fileName) => readFileSync(path.join(routesDirectory, fileName), 'utf8'))
      .join('\n');

    expect(routeSources).not.toMatch(/message:\s*(?:err|error)\?*\.message/);
  });

  test('a real Fastify route hides a poisoned network failure', async () => {
    const originalFetch = globalThis.fetch;
    const originalInternalToken = process.env.INTERNAL_SERVICE_TOKEN;
    process.env.INTERNAL_SERVICE_TOKEN = 'unit-test-internal-service-token-0001';
    globalThis.fetch = (async () => {
      throw new Error('connect ECONNREFUSED http://internal-user:secret@mcp-tools:8003');
    }) as typeof fetch;

    const app = Fastify({ logger: false });
    app.addHook('preHandler', async (request) => {
      request.user = {
        userId: 'user-proof',
        tenantId: 'tenant-proof',
        roles: ['member'],
        tokenBudget: 1_000,
        authType: 'session',
        scopes: [],
      };
    });

    try {
      await app.register(toolRoutes);
      const response = await app.inject({ method: 'GET', url: '/api/tools/list' });
      expect(response.statusCode).toBe(503);
      expect(response.json()).toEqual({
        error: 'Service Unavailable',
        message: 'The tool service is temporarily unavailable.',
      });
      expect(response.body).not.toContain('secret');
      expect(response.body).not.toContain('mcp-tools:8003');
    } finally {
      await app.close();
      globalThis.fetch = originalFetch;
      if (originalInternalToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
      else process.env.INTERNAL_SERVICE_TOKEN = originalInternalToken;
    }
  });
});
