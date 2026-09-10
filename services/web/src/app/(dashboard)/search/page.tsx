'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useMemo, useState } from 'react';
import { Activity, Brain, CheckSquare, FileText, Search } from 'lucide-react';

import {
  SearchScope,
  searchWorkspace,
  WorkspaceSearchResponse,
  WorkspaceSearchResult,
} from '@/lib/workspace-search';

const scopes: Array<{ value: SearchScope; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'documents', label: 'Documents' },
  { value: 'tasks', label: 'Tasks' },
  { value: 'memories', label: 'Memory' },
  { value: 'chat', label: 'Chat' },
];

const icons = {
  documents: FileText,
  tasks: CheckSquare,
  memories: Brain,
  chat: Activity,
};

function validScope(value: string | null): SearchScope {
  return scopes.some((scope) => scope.value === value) ? value as SearchScope : 'all';
}

function highlight(value: string, query: string) {
  if (!query) return value;
  const escapedQuery = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const expression = new RegExp(`(${escapedQuery})`, 'ig');
  return value.split(expression).map((part, index) => (
    part.toLocaleLowerCase() === query.toLocaleLowerCase()
      ? <mark key={`${part}-${index}`} className="rounded-sm bg-amber-300/20 px-0.5 text-amber-100">{part}</mark>
      : part
  ));
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? 'Unknown date'
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
}

function SearchContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const query = (searchParams.get('q') || '').trim();
  const scope = validScope(searchParams.get('scope'));
  const [input, setInput] = useState(query);
  const [data, setData] = useState<WorkspaceSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setInput(query), [query]);

  useEffect(() => {
    if (query.length < 2) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    searchWorkspace(query, { scope: 'all', limitPerType: 25, signal: controller.signal })
      .then(setData)
      .catch((loadError) => {
        if (loadError instanceof DOMException && loadError.name === 'AbortError') return;
        setData(null);
        setError(loadError instanceof Error ? loadError.message : 'Workspace search could not be completed.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [query]);

  const visibleResults = useMemo(
    () => data?.results.filter((result) => scope === 'all' || result.type === scope) ?? [],
    [data, scope],
  );
  const totalResults = data
    ? Object.values(data.facets).reduce((total, facet) => total + facet.count, 0)
    : 0;

  const navigate = (nextQuery: string, nextScope: SearchScope) => {
    const params = new URLSearchParams();
    if (nextQuery.trim()) params.set('q', nextQuery.trim());
    if (nextScope !== 'all') params.set('scope', nextScope);
    router.push(`/search${params.size ? `?${params}` : ''}`);
  };

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-5">
      <div className="pb-4 border-b border-zinc-800">
        <h1 className="text-xl font-semibold text-white tracking-tight">Workspace Search</h1>
        <p className="text-xs text-zinc-400 mt-0.5">Search persisted documents, tasks, memory, and chat runs in this workspace.</p>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          navigate(input, scope);
        }}
        className="relative"
      >
        <Search className="absolute left-3.5 top-1/2 w-4 h-4 -translate-y-1/2 text-zinc-500" />
        <input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Search your workspace…"
          aria-label="Workspace search query"
          autoFocus
          className="w-full rounded-xl border border-zinc-800 bg-zinc-950 py-3 pl-10 pr-24 text-sm text-white placeholder-zinc-600 outline-none focus:border-zinc-600"
        />
        <button
          type="submit"
          disabled={input.trim().length < 2}
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded-lg bg-white px-3 py-1.5 text-xs font-medium text-black disabled:opacity-40"
        >
          Search
        </button>
      </form>

      <div className="flex gap-1 overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950 p-1">
        {scopes.map((item) => {
          const count = item.value === 'all' ? totalResults : data?.facets[item.value].count ?? 0;
          return (
            <button
              key={item.value}
              type="button"
              onClick={() => navigate(query, item.value)}
              className={`flex items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-xs transition-colors ${
                scope === item.value ? 'bg-zinc-800 text-white' : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {item.label}
              {data && <span className="font-mono text-[10px] text-zinc-500">{count}</span>}
            </button>
          );
        })}
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">{error}</div>
      )}

      {query.length < 2 ? (
        <EmptyState title="Enter at least two characters" detail="Results stay scoped to your active workspace and account." />
      ) : loading ? (
        <div className="rounded-xl border border-zinc-800 bg-zinc-950 p-10 text-center text-xs text-zinc-500">Searching indexed workspace data…</div>
      ) : visibleResults.length === 0 ? (
        <EmptyState title={`No results for “${query}”`} detail="Try fewer words or switch to a different result type." />
      ) : (
        <div className="space-y-2.5">
          {visibleResults.map((result) => <ResultCard key={`${result.type}-${result.id}`} result={result} query={query} />)}
          {scope !== 'all' && data?.facets[scope].has_more && (
            <p className="py-2 text-center text-[11px] text-zinc-600">
              Showing the first {data.facets[scope].returned} of {data.facets[scope].count} matches.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ResultCard({ result, query }: { result: WorkspaceSearchResult; query: string }) {
  const Icon = icons[result.type];
  return (
    <Link href={result.href} className="block rounded-xl border border-zinc-800 bg-zinc-950 p-4 transition-colors hover:border-zinc-700 hover:bg-zinc-900/45">
      <div className="flex items-start gap-3">
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-2 text-zinc-400"><Icon className="w-4 h-4" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-medium text-white">{highlight(result.title, query)}</h2>
            <span className="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[10px] capitalize text-zinc-500">{result.type}</span>
          </div>
          {result.snippet && <p className="mt-1.5 text-xs leading-relaxed text-zinc-400">{highlight(result.snippet, query)}</p>}
          <p className="mt-2 text-[10px] text-zinc-600">{formatDate(result.created_at)}</p>
        </div>
      </div>
    </Link>
  );
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-950/60 p-10 text-center">
      <Search className="mx-auto w-6 h-6 text-zinc-600" />
      <p className="mt-3 text-sm text-zinc-300">{title}</p>
      <p className="mt-1 text-xs text-zinc-600">{detail}</p>
    </div>
  );
}

export default function SearchPage() {
  return (
    <Suspense fallback={<div className="p-10 text-center text-xs text-zinc-500">Loading search…</div>}>
      <SearchContent />
    </Suspense>
  );
}

