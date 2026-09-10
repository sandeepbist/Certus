export type GatewayRequestProfile = 'interactive' | 'processing' | 'stream';

type GatewayFetchOptions = {
  profile?: GatewayRequestProfile;
  timeoutMs?: number;
};

const gatewayRequestTimeouts: Record<GatewayRequestProfile, number> = {
  interactive: 20_000,
  processing: 195_000,
  stream: 615_000,
};

export function gatewayRequestSignal(
  profile: GatewayRequestProfile,
  callerSignal?: AbortSignal | null,
  timeoutMs: number = gatewayRequestTimeouts[profile],
): AbortSignal {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 900_000) {
    throw new Error('Gateway request timeout must be an integer between 100 and 900000 ms.');
  }
  const deadline = AbortSignal.timeout(timeoutMs);
  return callerSignal ? AbortSignal.any([callerSignal, deadline]) : deadline;
}

export function gatewayFetch(
  path: string,
  init: RequestInit = {},
  options: GatewayFetchOptions = {},
) {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const profile = options.profile || 'interactive';
  return fetch(`/api/gateway${normalizedPath}`, {
    ...init,
    credentials: 'same-origin',
    signal: gatewayRequestSignal(profile, init.signal, options.timeoutMs),
  });
}

export function gatewayWebSocketUrl(path = '/chat') {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const configuredUrl = process.env.NEXT_PUBLIC_GATEWAY_WS_URL;
  if (configuredUrl) {
    return `${configuredUrl.replace(/\/$/, '')}${normalizedPath}`;
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/gateway/ws${normalizedPath}`;
}
