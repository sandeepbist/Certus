'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { AlertCircle, BarChart3, Coins, Cpu, FileText, Layers, Zap } from 'lucide-react';
import { gatewayFetch } from '@/lib/gateway-client';

type Analytics = {
  period_days: number;
  overview: {
    total_queries: number;
    total_tokens: number;
    total_cost_usd: number;
    avg_latency_ms: number | null;
    avg_claim_evidence_integrity: number | null;
    queries_today: number;
    queries_yesterday: number;
    query_change_percent: number | null;
  };
  model_distribution: Array<{
    model_used: string;
    count: number;
    tokens: number;
    cost_usd: number;
  }>;
  latency_percentiles: { p50_ms: number | null; p95_ms: number | null; p99_ms: number | null };
  daily_budget: {
    allocated_tokens: number;
    used_tokens: number;
    reserved_tokens: number;
    remaining_tokens: number;
  };
  daily_series: Array<{ date: string; queries: number; tokens: number; cost_usd: number }>;
  document_stats: { total_documents: number; ready_documents: number; storage_bytes: number; chunks: number };
};

const formatLatency = (value: number | null) => (value === null ? '—' : `${value.toLocaleString()}ms`);

function AnalyticsContent() {
  const [stats, setStats] = useState<Analytics | null>(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadUsage() {
      setLoading(true);
      setError(null);
      try {
        const response = await gatewayFetch(`/analytics/usage?days=${days}`);
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(payload?.message || payload?.detail || 'Usage analytics could not be loaded.');
        }
        if (!cancelled) setStats(payload as Analytics);
      } catch (loadError) {
        if (!cancelled) {
          setStats(null);
          setError(loadError instanceof Error ? loadError.message : 'Usage analytics could not be loaded.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadUsage();
    return () => {
      cancelled = true;
    };
  }, [days]);

  const maxDailyTokens = useMemo(
    () => Math.max(1, ...(stats?.daily_series.map((point) => point.tokens) ?? [1])),
    [stats],
  );
  const budgetPercent = stats
    ? Math.min(100, Math.round((
      (stats.daily_budget.used_tokens + stats.daily_budget.reserved_tokens)
      / Math.max(1, stats.daily_budget.allocated_tokens)
    ) * 100))
    : 0;

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div className="pb-4 border-b border-zinc-800 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">Analytics & Usage</h1>
          <p className="text-xs text-zinc-400 mt-0.5">Measured usage, cost, latency, and lexical evidence coverage from agent runs.</p>
        </div>
        <label className="text-[11px] text-zinc-500">
          Reporting period
          <select
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="block mt-1 px-2.5 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </label>
      </div>

      {error && (
        <div role="alert" className="p-3 rounded-lg border border-red-900/60 bg-red-950/30 text-xs text-red-300 flex gap-2">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading && !stats ? (
        <div className="p-10 rounded-xl bg-zinc-950 border border-zinc-800 text-center text-xs text-zinc-500">Loading measured usage…</div>
      ) : stats ? (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricCard label="Queries" value={stats.overview.total_queries.toLocaleString()} icon={Cpu} detail={`${stats.overview.queries_today} today`} />
            <MetricCard label="Tokens" value={stats.overview.total_tokens.toLocaleString()} icon={Zap} detail={`${stats.daily_budget.remaining_tokens.toLocaleString()} left today`} />
            <MetricCard label="Estimated spend" value={`$${stats.overview.total_cost_usd.toFixed(4)}`} icon={Coins} detail={`${stats.period_days}-day total`} />
            <MetricCard
              label="Claim-link integrity"
              value={stats.overview.avg_claim_evidence_integrity === null ? '—' : `${(stats.overview.avg_claim_evidence_integrity * 100).toFixed(0)}%`}
              icon={Layers}
              detail={stats.overview.avg_claim_evidence_integrity === null ? 'No v1 grounded runs yet' : 'Mechanical only; not semantic entailment'}
              accent={false}
            />
          </div>

          <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-xs font-medium text-white">Daily token budget</h3>
                <p className="text-[11px] text-zinc-500">
                  {stats.daily_budget.used_tokens.toLocaleString()} of {stats.daily_budget.allocated_tokens.toLocaleString()} tokens used today
                  {stats.daily_budget.reserved_tokens > 0
                    ? ` · ${stats.daily_budget.reserved_tokens.toLocaleString()} held by active runs`
                    : ''}
                </p>
              </div>
              <span className="text-xs font-mono font-semibold text-zinc-200">{budgetPercent}%</span>
            </div>
            <div className="w-full h-2 rounded-full bg-zinc-900 overflow-hidden">
              <div style={{ width: `${budgetPercent}%` }} className="h-full bg-white rounded-full transition-all" />
            </div>
          </div>

          <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-medium text-white">Token usage over time</h3>
              <span className="text-[10px] text-zinc-500">UTC calendar days</span>
            </div>
            <div className="h-32 flex items-end gap-1" aria-label={`Daily token usage for the last ${stats.period_days} days`}>
              {stats.daily_series.map((point) => (
                <div key={point.date} className="group flex-1 h-full flex items-end relative" title={`${point.date}: ${point.tokens.toLocaleString()} tokens, ${point.queries} queries`}>
                  <div
                    className="w-full min-h-px rounded-t bg-zinc-600 group-hover:bg-white transition-colors"
                    style={{ height: `${Math.max(point.tokens > 0 ? 3 : 0, (point.tokens / maxDailyTokens) * 100)}%` }}
                  />
                </div>
              ))}
            </div>
            <div className="flex justify-between text-[10px] text-zinc-600 font-mono">
              <span>{stats.daily_series[0]?.date}</span>
              <span>{stats.daily_series.at(-1)?.date}</span>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-3">
              <h3 className="text-xs font-medium text-white">Response latency</h3>
              {(['p50_ms', 'p95_ms', 'p99_ms'] as const).map((key) => (
                <div key={key} className="flex items-center justify-between p-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs font-mono">
                  <span className="text-zinc-400">{key.replace('_ms', '').toUpperCase()}</span>
                  <span className="text-zinc-200 font-medium">{formatLatency(stats.latency_percentiles[key])}</span>
                </div>
              ))}
              <p className="text-[10px] text-zinc-600">Average: {formatLatency(stats.overview.avg_latency_ms)}</p>
            </div>

            <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-3">
              <h3 className="text-xs font-medium text-white flex items-center gap-1.5"><BarChart3 className="w-3.5 h-3.5" /> Model traffic</h3>
              {stats.model_distribution.length === 0 ? (
                <p className="py-6 text-center text-xs text-zinc-500">No agent runs in this period.</p>
              ) : stats.model_distribution.map((model) => (
                <div key={model.model_used} className="p-2.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <span className="font-mono text-zinc-200 font-medium truncate block">{model.model_used}</span>
                    <p className="text-[10px] text-zinc-500 font-mono">{model.tokens.toLocaleString()} tokens · ${model.cost_usd.toFixed(4)}</p>
                  </div>
                  <span className="px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 font-mono text-[10px] whitespace-nowrap">{model.count} runs</span>
                </div>
              ))}
            </div>
          </div>

          <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Summary label="Documents" value={stats.document_stats.total_documents} icon={FileText} />
            <Summary label="Ready" value={stats.document_stats.ready_documents} />
            <Summary label="Chunks" value={stats.document_stats.chunks} />
            <Summary label="Storage" value={`${(stats.document_stats.storage_bytes / 1024 / 1024).toFixed(2)} MB`} />
          </div>
        </>
      ) : null}
    </div>
  );
}

function MetricCard({ label, value, detail, icon: Icon, accent = false }: { label: string; value: string; detail: string; icon: React.ComponentType<{ className?: string }>; accent?: boolean }) {
  return (
    <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-1.5">
      <div className="flex items-center justify-between text-zinc-400 text-xs"><span>{label}</span><Icon className="w-3.5 h-3.5" /></div>
      <div className={`text-xl font-semibold font-mono ${accent ? 'text-emerald-400' : 'text-white'}`}>{value}</div>
      <span className="text-[10px] text-zinc-500 font-mono">{detail}</span>
    </div>
  );
}

function Summary({ label, value, icon: Icon }: { label: string; value: string | number; icon?: React.ComponentType<{ className?: string }> }) {
  return (
    <div>
      <span className="text-[10px] text-zinc-500 flex items-center gap-1">{Icon && <Icon className="w-3 h-3" />}{label}</span>
      <p className="mt-1 text-sm font-mono text-zinc-200">{typeof value === 'number' ? value.toLocaleString() : value}</p>
    </div>
  );
}

export default function AnalyticsPage() {
  return <AnalyticsContent />;
}
