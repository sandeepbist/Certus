'use client';

import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { ArrowRight, Search } from 'lucide-react';

import { gatewayFetch } from '@/lib/gateway-client';

interface TraceItem {
  id: string;
  input_query: string;
  model_used: string | null;
  total_tokens: number;
  estimated_cost_usd: number | null;
  latency_ms: number | null;
  eval_score: number | null;
  answer_status: string;
  grounding_profile: string;
  replay_of_run_id: string | null;
  replay_mode: 'original' | 'frozen_evidence' | 'fresh_retrieval';
  status: string;
  created_at: string;
  completed_at: string | null;
}

type TraceResponse = {
  traces: TraceItem[];
  pagination: { limit: number; next_cursor: string | null };
  available_models: string[];
};

const pageSize = 25;

function TracesContent() {
  const [traces, setTraces] = useState<TraceItem[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedModel, setSelectedModel] = useState('');
  const [selectedStatus, setSelectedStatus] = useState('');
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadTraces = useCallback(async (pageCursor?: string, signal?: AbortSignal) => {
      if (pageCursor) setLoadingMore(true);
      else setLoading(true);
      const params = new URLSearchParams({ limit: String(pageSize) });
      if (searchQuery.trim()) params.set('q', searchQuery.trim());
      if (selectedModel) params.set('model', selectedModel);
      if (selectedStatus) params.set('status', selectedStatus);
      if (pageCursor) params.set('cursor', pageCursor);
      try {
        setError(null);
        const response = await gatewayFetch('/traces?' + params.toString(), { signal });
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Traces could not be loaded.');
        const data = payload as TraceResponse;
        setTraces((current) => {
          if (!pageCursor) return data.traces;
          const existingIds = new Set(current.map((trace) => trace.id));
          return [...current, ...data.traces.filter((trace) => !existingIds.has(trace.id))];
        });
        setModels(data.available_models);
        setNextCursor(data.pagination.next_cursor);
      } catch (loadError) {
        if (loadError instanceof DOMException && loadError.name === 'AbortError') return;
        if (!pageCursor) setTraces([]);
        setError(loadError instanceof Error ? loadError.message : 'Traces could not be loaded.');
      } finally {
        if (pageCursor) setLoadingMore(false);
        else setLoading(false);
      }
  }, [searchQuery, selectedModel, selectedStatus]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadTraces(undefined, controller.signal);
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadTraces]);

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">Agent traces</h1>
          <p className="text-xs text-zinc-400 mt-0.5">Persisted run inputs, outputs, atomic evidence-link diagnostics, costs, and execution events.</p>
        </div>
        <span className="text-xs font-mono px-2.5 py-1 rounded-md bg-zinc-900 border border-zinc-800 text-zinc-400">
          Loaded runs: <strong className="text-white">{traces.length}</strong>
        </span>
      </div>

      {error && <div role="alert" className="p-3 rounded-lg border border-red-900/60 bg-red-950/30 text-xs text-red-300">{error}</div>}

      <div className="grid grid-cols-1 sm:grid-cols-[1fr_auto_auto] gap-2.5">
        <div className="relative">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search run inputs and outputs…" className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600" />
        </div>
        <select value={selectedModel} onChange={(event) => setSelectedModel(event.target.value)} className="bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 rounded-lg px-2.5 py-2">
          <option value="">All models</option>
          {models.map((model) => <option key={model} value={model}>{model}</option>)}
        </select>
        <select value={selectedStatus} onChange={(event) => setSelectedStatus(event.target.value)} className="bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 rounded-lg px-2.5 py-2">
          <option value="">All statuses</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="running">Running</option>
          <option value="timeout">Timed out</option>
        </select>
      </div>

      <div className="bg-zinc-950 border border-zinc-800/80 rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-zinc-800 bg-zinc-900/60 text-zinc-400 font-mono text-[10px] uppercase">
                <th className="py-3 px-4 font-medium">Input / run</th>
                <th className="py-3 px-4 font-medium">Model</th>
                <th className="py-3 px-4 font-medium">Latency</th>
                <th className="py-3 px-4 font-medium">Tokens</th>
                <th className="py-3 px-4 font-medium">Cost</th>
                <th className="py-3 px-4 font-medium">Evidence integrity</th>
                <th className="py-3 px-4 font-medium text-right">Inspect</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/70">
              {loading ? (
                <tr><td colSpan={7} className="py-10 text-center text-zinc-500">Loading traces…</td></tr>
              ) : traces.length === 0 ? (
                <tr><td colSpan={7} className="py-10 text-center text-zinc-500">No runs match these filters.</td></tr>
              ) : traces.map((trace) => (
                <tr key={trace.id} className="hover:bg-zinc-900/40 transition-colors">
                  <td className="py-3 px-4">
                    <div className="font-medium text-white truncate max-w-md">{trace.input_query}</div>
                    <div className="text-[10px] text-zinc-500 font-mono mt-0.5">
                      {trace.id} · {trace.status} · {trace.replay_mode.replaceAll('_', ' ')}
                    </div>
                  </td>
                  <td className="py-3 px-4 text-zinc-300 font-mono text-[10px]">{trace.model_used || '—'}</td>
                  <td className="py-3 px-4 text-zinc-300 font-mono">{trace.latency_ms == null ? '—' : trace.latency_ms.toLocaleString() + 'ms'}</td>
                  <td className="py-3 px-4 text-zinc-300 font-mono">{trace.total_tokens.toLocaleString()}</td>
                  <td className="py-3 px-4 text-zinc-300 font-mono">{trace.estimated_cost_usd == null ? '—' : '$' + Number(trace.estimated_cost_usd).toFixed(4)}</td>
                  <td className="py-3 px-4 text-zinc-300 font-mono">
                    {trace.grounding_profile === 'certus_atomic_claim_evidence:v1' && trace.eval_score != null
                      ? (Number(trace.eval_score) * 100).toFixed(0) + '%'
                      : 'legacy'}
                  </td>
                  <td className="py-3 px-4 text-right">
                    <Link href={'/traces/' + trace.id} className="inline-flex items-center gap-1 px-2 py-1 rounded bg-zinc-900 hover:bg-zinc-800 text-zinc-200 border border-zinc-800">
                      Details <ArrowRight className="w-3 h-3" />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {nextCursor && (
        <div className="flex justify-center">
          <button type="button" onClick={() => void loadTraces(nextCursor)} disabled={loadingMore} className="px-4 py-2 rounded bg-zinc-900 border border-zinc-800 disabled:opacity-40 text-xs text-zinc-300 hover:text-white">
            {loadingMore ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  );
}

export default function TracesPage() {
  return <TracesContent />;
}
