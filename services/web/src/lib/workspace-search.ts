import { gatewayFetch } from '@/lib/gateway-client';

export type SearchScope = 'all' | 'documents' | 'tasks' | 'memories' | 'chat';

export type WorkspaceSearchResult = {
  type: Exclude<SearchScope, 'all'>;
  id: string;
  title: string;
  snippet: string;
  href: string;
  metadata: Record<string, unknown>;
  created_at: string;
  rank: number;
};

export type WorkspaceSearchResponse = {
  query: string;
  scope: SearchScope;
  results: WorkspaceSearchResult[];
  facets: Record<Exclude<SearchScope, 'all'>, {
    count: number;
    returned: number;
    has_more: boolean;
  }>;
};

export async function searchWorkspace(
  query: string,
  options: { scope?: SearchScope; limitPerType?: number; signal?: AbortSignal } = {},
) {
  const params = new URLSearchParams({
    q: query,
    scope: options.scope || 'all',
    limit_per_type: String(options.limitPerType || 8),
  });
  const response = await gatewayFetch(`/search?${params}`, { signal: options.signal });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : null;
    throw new Error(payload?.message || detail || 'Workspace search could not be completed.');
  }
  return payload as WorkspaceSearchResponse;
}

