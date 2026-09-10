type JsonObject = Record<string, unknown>;

type Probe = {
  name: string;
  url: string;
  validate?: (body: JsonObject) => boolean;
};

// Next development mode may compile the first requested route on demand.
const requestTimeoutMs = Number(process.env.SMOKE_REQUEST_TIMEOUT_MS || 15_000);

if (!Number.isFinite(requestTimeoutMs) || requestTimeoutMs < 100 || requestTimeoutMs > 60_000) {
  throw new Error('SMOKE_REQUEST_TIMEOUT_MS must be between 100 and 60000 milliseconds.');
}

const probes: Probe[] = [
  { name: 'web', url: 'http://127.0.0.1:3000/' },
  {
    name: 'web readiness',
    url: 'http://127.0.0.1:3000/api/health/ready',
    validate: (body) => body.status === 'ready' && body.service === 'web',
  },
  {
    name: 'gateway liveness',
    url: 'http://127.0.0.1:4000/health',
    validate: (body) => body.status === 'ok' && body.service === 'gateway',
  },
  {
    name: 'gateway readiness',
    url: 'http://127.0.0.1:4000/health/ready',
    validate: (body) => body.status === 'ready',
  },
  {
    name: 'ingestion liveness',
    url: 'http://127.0.0.1:8001/health',
    validate: (body) => body.status === 'ok' && body.service === 'ingestion',
  },
  {
    name: 'ingestion readiness',
    url: 'http://127.0.0.1:8001/health/ready',
    validate: (body) => body.status === 'ready' && body.service === 'ingestion',
  },
  { name: 'original object storage', url: 'http://127.0.0.1:9000/health' },
  {
    name: 'orchestration liveness',
    url: 'http://127.0.0.1:8002/health',
    validate: (body) => body.status === 'ok' && body.service === 'orchestration',
  },
  {
    name: 'orchestration readiness',
    url: 'http://127.0.0.1:8002/health/ready',
    validate: (body) => body.status === 'ready' && body.service === 'orchestration',
  },
  {
    name: 'MCP tools liveness',
    url: 'http://127.0.0.1:8003/health',
    validate: (body) => body.status === 'ok' && body.service === 'mcp-tools',
  },
  {
    name: 'MCP tools readiness',
    url: 'http://127.0.0.1:8003/health/ready',
    validate: (body) => body.status === 'ready' && body.service === 'mcp-tools',
  },
  { name: 'Temporal UI', url: 'http://127.0.0.1:8233/' },
];

async function probeService(probe: Probe): Promise<void> {
  const response = await fetch(probe.url, {
    redirect: 'error',
    signal: AbortSignal.timeout(requestTimeoutMs),
  });
  if (!response.ok) {
    throw new Error(`${probe.name} returned HTTP ${response.status}`);
  }

  if (probe.validate) {
    const body = await response.json() as JsonObject;
    if (!probe.validate(body)) {
      throw new Error(`${probe.name} returned an unexpected health payload`);
    }
  } else {
    await response.body?.cancel();
  }
}

const results: PromiseSettledResult<void>[] = [];

// Next development mode compiles request-bound routes lazily. Qualify the web
// process and its database/auth boundary before the Gateway probes that depend
// on that readiness endpoint; the remaining independent probes stay parallel.
for (const probe of probes.slice(0, 2)) {
  try {
    await probeService(probe);
    results.push({ status: 'fulfilled', value: undefined });
  } catch (reason) {
    results.push({ status: 'rejected', reason });
  }
}
results.push(...await Promise.allSettled(probes.slice(2).map(probeService)));
let failed = 0;

for (const [index, result] of results.entries()) {
  const probe = probes[index];
  if (result.status === 'fulfilled') {
    console.log(`✓ ${probe.name}`);
    continue;
  }

  failed += 1;
  const reason = result.reason instanceof Error ? result.reason.message : String(result.reason);
  console.error(`✗ ${probe.name}: ${reason}`);
}

if (failed > 0) {
  console.error(`Local smoke check failed: ${failed}/${probes.length} probes failed.`);
  process.exit(1);
}

console.log(`Local smoke check passed: ${probes.length}/${probes.length} services healthy.`);
