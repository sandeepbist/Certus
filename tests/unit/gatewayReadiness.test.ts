import { describe, expect, test } from 'bun:test';

import {
  configuredReadinessTargets,
  probeReadinessTargets,
  type ReadinessTarget,
} from '../../services/gateway/src/utils/readiness';

const targets: ReadinessTarget[] = [
  { name: 'auth', url: 'http://auth/ready', service: 'web', status: 'ready' },
  {
    name: 'ingestion',
    url: 'http://ingestion/health/ready',
    service: 'ingestion',
    status: 'ready',
  },
  {
    name: 'orchestration',
    url: 'http://orchestration/health/ready',
    service: 'orchestration',
    status: 'ready',
  },
  {
    name: 'mcp',
    url: 'http://mcp/health/ready',
    service: 'mcp-tools',
    status: 'ready',
  },
];

function readinessFetch(overrides: Record<string, Response> = {}): typeof fetch {
  return (async (input: string | URL | Request) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const override = overrides[url];
    if (override) return override;
    const target = targets.find((candidate) => candidate.url === url);
    return Response.json({ status: target?.status, service: target?.service });
  }) as typeof fetch;
}

describe('gateway readiness probes', () => {
  test('aggregates internal readiness rather than process liveness', () => {
    const configured = configuredReadinessTargets();
    for (const target of configured.filter((candidate) => candidate.name !== 'auth')) {
      expect(target.url.endsWith('/health/ready')).toBeTrue();
      expect(target.status).toBe('ready');
    }
  });

  test('accepts only the expected healthy payload from every dependency', async () => {
    await expect(probeReadinessTargets(targets, readinessFetch())).resolves.toEqual({
      auth: 'ready',
      ingestion: 'ready',
      orchestration: 'ready',
      mcp: 'ready',
    });
  });

  test('fails closed on unavailable or misleading dependency responses', async () => {
    const result = await probeReadinessTargets(
      targets,
      readinessFetch({
        'http://auth/ready': Response.json(
          { status: 'not_ready', service: 'web' },
          { status: 503 },
        ),
        'http://ingestion/health/ready': Response.json({
          status: 'ready',
          service: 'wrong-service',
        }),
      }),
    );

    expect(result).toEqual({
      auth: 'not_ready',
      ingestion: 'not_ready',
      orchestration: 'ready',
      mcp: 'ready',
    });
  });
});
