import { createConnection } from 'node:net';
import { FastifyPluginAsync } from 'fastify';

import { orchestrationBreaker } from '../utils/circuitBreaker.js';
import { requireAuthContext } from '../utils/authContext.js';
import { observedInternalServiceFetch } from '../utils/internalService.js';
import { sendServiceUnavailable } from '../utils/serviceUnavailable.js';

const ORCHESTRATION_SERVICE_URL = process.env.ORCHESTRATION_SERVICE_URL || 'http://localhost:8002';
const INGESTION_SERVICE_URL = process.env.INGESTION_SERVICE_URL || 'http://localhost:8001';
const MCP_TOOLS_URL = process.env.MCP_TOOLS_URL || 'http://localhost:8003';

type HealthStatus = 'healthy' | 'unavailable';

async function probeHttp(name: string, url: string) {
  const started = performance.now();
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
    return {
      name,
      status: (response.ok ? 'healthy' : 'unavailable') as HealthStatus,
      latency_ms: Math.round(performance.now() - started),
    };
  } catch {
    return {
      name,
      status: 'unavailable' as HealthStatus,
      latency_ms: Math.round(performance.now() - started),
    };
  }
}

async function probeTcp(name: string, host: string, port: number) {
  const started = performance.now();
  return new Promise<{ name: string; status: HealthStatus; latency_ms: number }>((resolve) => {
    const socket = createConnection({ host, port });
    const finish = (status: HealthStatus) => {
      socket.destroy();
      resolve({ name, status, latency_ms: Math.round(performance.now() - started) });
    };
    socket.setTimeout(2_000);
    socket.once('connect', () => finish('healthy'));
    socket.once('timeout', () => finish('unavailable'));
    socket.once('error', () => finish('unavailable'));
  });
}

function urlTarget(value: string, fallbackPort: number) {
  try {
    const parsed = new URL(value);
    return { host: parsed.hostname || 'localhost', port: Number(parsed.port || fallbackPort) };
  } catch {
    return { host: 'localhost', port: fallbackPort };
  }
}

function addressTarget(value: string, fallbackPort: number) {
  const [host = 'localhost', port] = value.split(':');
  return { host, port: Number(port || fallbackPort) };
}

async function detailedHealth() {
  const postgres = urlTarget(
    process.env.DATABASE_URL || 'postgresql://localhost:5432/nexus',
    5432,
  );
  const redis = urlTarget(process.env.REDIS_URL || 'redis://localhost:6379', 6379);
  const neo4j = urlTarget(process.env.NEO4J_URI || 'bolt://localhost:7687', 7687);
  const temporal = addressTarget(process.env.TEMPORAL_ADDRESS || 'localhost:7233', 7233);
  const services = await Promise.all([
    probeHttp('Ingestion', `${INGESTION_SERVICE_URL}/health`),
    probeHttp('Orchestration', `${ORCHESTRATION_SERVICE_URL}/health`),
    probeHttp('MCP tools', `${MCP_TOOLS_URL}/health`),
    probeTcp('PostgreSQL', postgres.host, postgres.port),
    probeTcp('Redis', redis.host, redis.port),
    probeTcp('Neo4j', neo4j.host, neo4j.port),
    probeTcp('Temporal', temporal.host, temporal.port),
  ]);
  return {
    status: services.every((service) => service.status === 'healthy') ? 'healthy' : 'degraded',
    checked_at: new Date().toISOString(),
    services,
  };
}

export const dashboardRoutes: FastifyPluginAsync = async (fastify) => {
  fastify.get('/api/dashboard/overview', async (request, reply) => {
    const { tenantId, userId } = requireAuthContext(request);
    try {
      return await orchestrationBreaker.execute(async () => {
        const response = await observedInternalServiceFetch(
          orchestrationBreaker,
          `${ORCHESTRATION_SERVICE_URL}/dashboard/overview`,
          {},
          { tenantId, userId },
        );
        if (response.ok) return await response.json();
        return reply.status(response.status).send(await response.json());
      });
    } catch (error: unknown) {
      return sendServiceUnavailable(
        request,
        reply,
        'orchestration',
        'Dashboard data is temporarily unavailable.',
        error,
      );
    }
  });

  fastify.get('/api/dashboard/health', async (request) => {
    requireAuthContext(request);
    return detailedHealth();
  });
};
