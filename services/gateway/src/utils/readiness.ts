export type ReadinessTarget = {
  name: 'auth' | 'ingestion' | 'orchestration' | 'mcp';
  url: string;
  service: string;
  status: string;
};

export type ReadinessState = 'ready' | 'not_ready';

const readinessProbeTimeoutMs = Number(process.env.READINESS_PROBE_TIMEOUT_MS || 5_000);

if (
  !Number.isFinite(readinessProbeTimeoutMs)
  || readinessProbeTimeoutMs < 100
  || readinessProbeTimeoutMs > 10_000
) {
  throw new Error('READINESS_PROBE_TIMEOUT_MS must be between 100 and 10000 milliseconds.');
}

function serviceUrl(variableName: string, fallback: string) {
  const configured = process.env[variableName]?.trim() || fallback;
  return configured.replace(/\/$/, '');
}

export function configuredReadinessTargets(): ReadinessTarget[] {
  const authServiceUrl = (
    process.env.AUTH_SERVICE_URL?.trim()
    || process.env.BETTER_AUTH_URL?.trim()
    || 'http://localhost:3000'
  ).replace(/\/$/, '');
  return [
    {
      name: 'auth',
      url: `${authServiceUrl}/api/health/ready`,
      service: 'web',
      status: 'ready',
    },
    {
      name: 'ingestion',
      url: `${serviceUrl('INGESTION_SERVICE_URL', 'http://localhost:8001')}/health/ready`,
      service: 'ingestion',
      status: 'ready',
    },
    {
      name: 'orchestration',
      url: `${serviceUrl('ORCHESTRATION_SERVICE_URL', 'http://localhost:8002')}/health/ready`,
      service: 'orchestration',
      status: 'ready',
    },
    {
      name: 'mcp',
      url: `${serviceUrl('MCP_TOOLS_URL', 'http://localhost:8003')}/health/ready`,
      service: 'mcp-tools',
      status: 'ready',
    },
  ];
}

async function probeTarget(target: ReadinessTarget, fetchImpl: typeof fetch) {
  try {
    const response = await fetchImpl(target.url, {
      headers: { accept: 'application/json' },
      redirect: 'error',
      signal: AbortSignal.timeout(readinessProbeTimeoutMs),
    });
    if (!response.ok) return 'not_ready' as const;
    const body: unknown = await response.json();
    if (
      typeof body !== 'object'
      || body === null
      || !('status' in body)
      || !('service' in body)
      || body.status !== target.status
      || body.service !== target.service
    ) {
      return 'not_ready' as const;
    }
    return 'ready' as const;
  } catch {
    return 'not_ready' as const;
  }
}

export async function probeReadinessTargets(
  targets: ReadinessTarget[],
  fetchImpl: typeof fetch = fetch,
): Promise<Record<ReadinessTarget['name'], ReadinessState>> {
  const results = await Promise.all(
    targets.map(async (target) => [target.name, await probeTarget(target, fetchImpl)] as const),
  );
  return Object.fromEntries(results) as Record<ReadinessTarget['name'], ReadinessState>;
}
