'use client';

import React, { use, useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, Layers, RefreshCw } from 'lucide-react';

import { gatewayFetch } from '@/lib/gateway-client';
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents';

interface TraceDetailProps {
  params: Promise<{ runId: string }>;
}

type TraceEvent = {
  agent?: string;
  action?: string;
  status?: string;
  details?: Record<string, unknown>;
  timestamp?: number;
};

type Trace = {
  id: string;
  input_query: string;
  model_used: string | null;
  output_response: string | null;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost_usd: number | null;
  latency_ms: number | null;
  retrieval_latency_ms: number | null;
  llm_latency_ms: number | null;
  eval_score: number | null;
  answer_status: string;
  grounding_profile: string;
  evidence_manifest: Record<string, unknown> | null;
  evidence_manifest_db_sha256: string | null;
  generation_profile: Record<string, unknown> | null;
  generation_profile_db_sha256: string | null;
  replay_of_run_id: string | null;
  replay_mode: 'original' | 'frozen_evidence' | 'fresh_retrieval';
  claim_evidence: unknown[] | string | null;
  eval_details: Record<string, unknown> | null;
  critic_iterations: number;
  events: TraceEvent[] | string | null;
  status: string;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
};

type ReplayResult = {
  original_run_id: string;
  replayed_run_id: string;
  replay_mode: 'frozen_evidence' | 'fresh_retrieval';
  original_latency_ms: number | null;
  replayed_latency_ms: number;
  original_eval_score: number | null;
  replayed_eval_score: number | null;
  original_answer_status: string;
  replayed_answer_status: string;
  original_grounding_profile: string;
  replayed_grounding_profile: string;
  replayed_model_used: string;
  replayed_response: string;
  agent_events: TraceEvent[];
};

function parseEvents(value: Trace['events']): TraceEvent[] {
  if (Array.isArray(value)) return value;
  if (typeof value !== 'string') return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function arrayLength(value: unknown[] | string | null): number {
  if (Array.isArray(value)) return value.length;
  if (typeof value !== 'string') return 0;
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.length : 0;
  } catch {
    return 0;
  }
}

export default function TraceDetailPage({ params }: TraceDetailProps) {
  const { runId } = use(params);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>(['auto']);
  const [overrideModel, setOverrideModel] = useState('auto');
  const [replayMode, setReplayMode] = useState<'frozen_evidence' | 'fresh_retrieval'>('frozen_evidence');
  const [replayResult, setReplayResult] = useState<ReplayResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [isReplaying, setIsReplaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const refreshTrace = useCallback(() => {
    setRefreshKey((value) => value + 1);
  }, []);

  useRealtimeEvents(
    [`trace:${runId}`],
    refreshTrace,
    refreshTrace,
  );

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    async function loadDetail() {
      try {
        const response = await gatewayFetch('/traces/' + encodeURIComponent(runId), {
          signal: controller.signal,
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Trace could not be loaded.');
        if (!cancelled) {
          setTrace(payload.trace as Trace);
          setAvailableModels(payload.available_models || ['auto']);
          setError(null);
        }
      } catch (loadError) {
        if (!cancelled && !controller.signal.aborted) {
          setError(loadError instanceof Error ? loadError.message : 'Trace could not be loaded.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadDetail();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [runId, refreshKey]);

  const events = useMemo(() => parseEvents(trace?.events ?? null), [trace?.events]);

  const handleReplay = async () => {
    setIsReplaying(true);
    setReplayResult(null);
    setError(null);
    try {
      const response = await gatewayFetch('/traces/' + encodeURIComponent(runId) + '/replay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          override_model: overrideModel === 'auto' ? null : overrideModel,
          mode: replayMode,
        }),
      }, { profile: 'processing' });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Trace replay failed.');
      setReplayResult(payload as ReplayResult);
    } catch (replayError) {
      setError(replayError instanceof Error ? replayError.message : 'Trace replay failed.');
    } finally {
      setIsReplaying(false);
    }
  };

  if (loading) {
    return <div className="p-8 text-center text-xs text-zinc-500">Loading trace…</div>;
  }

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <Link href="/traces" className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-white mb-2"><ArrowLeft className="w-3 h-3" />Back to traces</Link>
          <h1 className="text-xl font-semibold text-white tracking-tight">Trace inspector</h1>
          <p className="text-xs font-mono text-zinc-500 mt-0.5 break-all">Run: {runId}</p>
        </div>
        {trace && (
          <div className="flex gap-2">
            <select value={replayMode} onChange={(event) => setReplayMode(event.target.value as typeof replayMode)} className="px-2.5 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-300" title="Frozen evidence reuses the exact retained generation pack; fresh retrieval searches the current library again.">
              <option value="frozen_evidence">Frozen evidence</option>
              <option value="fresh_retrieval">Fresh retrieval</option>
            </select>
            <select value={overrideModel} onChange={(event) => setOverrideModel(event.target.value)} className="px-2.5 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-300">
              {availableModels.map((model) => (
                <option key={model} value={model}>
                  {model === 'auto'
                    ? (availableModels.length === 1 ? 'Local extractive / auto' : 'Saved/auto model')
                    : model}
                </option>
              ))}
            </select>
            <button type="button" onClick={handleReplay} disabled={isReplaying} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black disabled:opacity-40 text-xs font-medium">
              <RefreshCw className={'w-3 h-3 ' + (isReplaying ? 'animate-spin' : '')} />
              {isReplaying ? 'Replaying…' : 'Replay as new run'}
            </button>
          </div>
        )}
      </div>

      {error && <div role="alert" className="p-3 rounded-lg border border-red-900/60 bg-red-950/30 text-xs text-red-300">{error}</div>}

      {!trace ? (
        <div className="p-8 rounded-xl bg-zinc-950 border border-zinc-800 text-center text-xs text-zinc-500">Trace unavailable.</div>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            <Fact label="Status" value={trace.status} />
            <Fact label="Model" value={trace.model_used || '—'} />
            <Fact label="Latency" value={trace.latency_ms == null ? '—' : trace.latency_ms.toLocaleString() + 'ms'} />
            <Fact label="Tokens" value={trace.total_tokens.toLocaleString()} />
            <Fact label="Answer status" value={trace.answer_status.replaceAll('_', ' ')} />
            <Fact label="Atomic claims" value={String(arrayLength(trace.claim_evidence))} />
            <Fact label="Evidence integrity" value={trace.grounding_profile === 'certus_atomic_claim_evidence:v1' && trace.eval_score != null ? (Number(trace.eval_score) * 100).toFixed(0) + '% (mechanical)' : 'Legacy metric unavailable'} />
            <Fact label="Replay mode" value={trace.replay_mode.replaceAll('_', ' ')} />
            <Fact label="Evidence pack" value={trace.evidence_manifest?.profile === 'certus_typed_evidence_manifest:v1' ? String(trace.evidence_manifest.source_count ?? 0) + ' frozen sources' : 'Legacy unavailable'} />
            <Fact label="Model revision" value={trace.generation_profile?.model_revision_locked === true ? 'Snapshot locked' : 'Alias / local'} />
          </div>

          <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-3">
            <div>
              <p className="text-[10px] text-zinc-500 font-mono uppercase">Input</p>
              <p className="text-xs font-medium text-white mt-1 whitespace-pre-wrap">{trace.input_query}</p>
            </div>
            <div className="pt-3 border-t border-zinc-800">
              <p className="text-[10px] text-zinc-500 font-mono uppercase">Output</p>
              <div className="mt-1.5 p-3 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 whitespace-pre-wrap leading-relaxed">
                {trace.output_response || (trace.error_message ? 'Error: ' + trace.error_message : 'No output was recorded.')}
              </div>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-3 border-t border-zinc-800 text-[10px] text-zinc-500 font-mono">
              <span>Prompt: {trace.prompt_tokens.toLocaleString()}</span>
              <span>Completion: {trace.completion_tokens.toLocaleString()}</span>
              <span>Retrieval: {trace.retrieval_latency_ms == null ? '—' : trace.retrieval_latency_ms + 'ms'}</span>
              <span>Cost: {trace.estimated_cost_usd == null ? '—' : '$' + Number(trace.estimated_cost_usd).toFixed(6)}</span>
            </div>
          </div>

          {replayResult && (
            <div className="p-4 rounded-xl bg-zinc-950 border border-emerald-900/50 space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold text-white">New replay run</h3>
                <Link href={'/traces/' + replayResult.replayed_run_id} className="text-[10px] text-emerald-400 hover:text-emerald-300 font-mono">{replayResult.replayed_run_id}</Link>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
                <Comparison label="Original latency" value={replayResult.original_latency_ms == null ? '—' : replayResult.original_latency_ms + 'ms'} />
                <Comparison label="Replay latency" value={replayResult.replayed_latency_ms + 'ms'} />
                <Comparison label="Replay mode" value={replayResult.replay_mode.replaceAll('_', ' ')} />
                <Comparison label="Replay model" value={replayResult.replayed_model_used} />
                <Comparison label="Replay answer status" value={replayResult.replayed_answer_status.replaceAll('_', ' ')} />
                <Comparison label="Replay evidence integrity" value={replayResult.replayed_grounding_profile === 'certus_atomic_claim_evidence:v1' && replayResult.replayed_eval_score != null ? (Number(replayResult.replayed_eval_score) * 100).toFixed(0) + '% (mechanical)' : 'Legacy metric unavailable'} />
              </div>
              <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 whitespace-pre-wrap">{replayResult.replayed_response}</div>
            </div>
          )}

          <div className="space-y-3">
            <h3 className="text-xs font-medium text-white flex items-center gap-1.5"><Layers className="w-3.5 h-3.5 text-zinc-400" />Execution events ({events.length})</h3>
            {events.length === 0 ? (
              <div className="p-6 rounded-xl border border-dashed border-zinc-800 text-center text-xs text-zinc-500">No execution events were recorded.</div>
            ) : events.map((event, index) => (
              <div key={index} className="p-3.5 rounded-xl bg-zinc-950 border border-zinc-800/80 text-xs flex items-start gap-3">
                <div className="w-6 h-6 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 font-mono text-[11px] flex items-center justify-center shrink-0">{index + 1}</div>
                <div className="flex-1 min-w-0 space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono uppercase text-zinc-300 font-medium">[{event.agent || 'system'}]</span>
                    <span className="text-[10px] text-zinc-500 font-mono">{event.status || 'recorded'}</span>
                  </div>
                  <p className="text-zinc-300">{event.action || 'No action description.'}</p>
                  {event.details && Object.keys(event.details).length > 0 && (
                    <pre className="p-2 rounded bg-zinc-900 border border-zinc-800 text-[10px] font-mono text-zinc-400 overflow-x-auto">{JSON.stringify(event.details, null, 2)}</pre>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Fact({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="p-3.5 rounded-xl bg-zinc-950 border border-zinc-800/80">
      <span className="text-[10px] text-zinc-500">{label}</span>
      <div className={'text-sm font-semibold font-mono mt-1 truncate ' + (accent ? 'text-emerald-400' : 'text-white')}>{value}</div>
    </div>
  );
}

function Comparison({ label, value }: { label: string; value: string }) {
  return <div><span className="text-zinc-500 text-[10px]">{label}</span><p className="text-zinc-100 mt-0.5">{value}</p></div>;
}
