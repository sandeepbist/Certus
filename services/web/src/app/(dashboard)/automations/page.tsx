'use client';

import React, { useState, useEffect, useCallback, Suspense } from 'react';
import Link from 'next/link';
import { gatewayFetch } from '@/lib/gateway-client';
import {
  Zap,
  Plus,
  Trash2,
  Power,
} from 'lucide-react';

interface AutomationRule {
  id: string;
  name: string;
  trigger_type: string;
  condition_expression: string;
  action_type: string;
  is_active: boolean;
  execution_count: number;
  version: number;
  created_at: string;
}

function AutomationsContent() {
  const [rules, setRules] = useState<AutomationRule[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const loadRules = useCallback(async (pageCursor?: string, signal?: AbortSignal) => {
    if (pageCursor) setIsLoadingMore(true);
    else setIsLoading(true);
    try {
      const query = new URLSearchParams({ limit: '50' });
      if (pageCursor) query.set('cursor', pageCursor);
      const res = await gatewayFetch(`/automations?${query}`, { signal });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.detail || 'Automations could not be loaded.');
      const page = Array.isArray(data.rules) ? data.rules as AutomationRule[] : [];
      setRules((current) => {
        if (!pageCursor) return page;
        const existingIds = new Set(current.map((rule) => rule.id));
        return [...current, ...page.filter((rule) => !existingIds.has(rule.id))];
      });
      setNextCursor(data.pagination?.next_cursor || null);
      setErrorMessage(null);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setErrorMessage(error instanceof Error ? error.message : 'Automations could not be loaded.');
    } finally {
      if (pageCursor) setIsLoadingMore(false);
      else setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadRules(undefined, controller.signal);
    return () => controller.abort();
  }, [loadRules]);

  const toggleActive = async (rule: AutomationRule) => {
    try {
      const response = await gatewayFetch(`/automations/${rule.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !rule.is_active, version: rule.version }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || 'Automation could not be updated.');
      setRules((previous) => previous.map((item) => (
        item.id === rule.id
          ? { ...item, is_active: data.rule.is_active, version: data.rule.version }
          : item
      )));
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Automation could not be updated.');
    }
  };

  const deleteRule = async (id: string) => {
    try {
      const response = await gatewayFetch(`/automations/${id}`, { method: 'DELETE' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Automation could not be deleted.');
      await loadRules();
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Automation could not be deleted.');
    }
  };

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">
            Automations
          </h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            Stored document triggers executed durably by Temporal workers.
          </p>
        </div>

        <Link
          href="/automations/new"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black hover:bg-zinc-200 text-xs font-medium transition-colors shrink-0"
        >
          <Plus className="w-3.5 h-3.5" />
          <span>New Rule</span>
        </Link>
      </div>

      {errorMessage && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {errorMessage}
        </div>
      )}

      {/* Rules List */}
      <div className="space-y-3">
        {isLoading && (
          <div className="p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            Loading automations...
          </div>
        )}
        {!isLoading && rules.length === 0 && (
          <div className="p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            No automation rules configured.
          </div>
        )}
        {rules.map((r) => (
          <div
            key={r.id}
            className={`p-4 rounded-xl border transition-colors flex items-center justify-between gap-4 ${
              r.is_active ? 'bg-zinc-950 border-zinc-800/80' : 'bg-zinc-950/60 border-zinc-850 opacity-60'
            }`}
          >
            <div className="flex items-start gap-3 min-w-0">
              <Zap className="w-4 h-4 text-zinc-400 shrink-0 mt-0.5" />
              <div className="min-w-0">
                <h3 className="text-xs font-medium text-white truncate">{r.name}</h3>
                <div className="flex flex-wrap items-center gap-1.5 mt-1.5 font-mono text-[10px]">
                  <span className="px-1.5 py-0.2 rounded bg-zinc-900 border border-zinc-800 text-zinc-300">
                    {r.trigger_type}
                  </span>
                  <span className="text-zinc-600">→</span>
                  <span className="px-1.5 py-0.2 rounded bg-zinc-900 border border-zinc-800 text-zinc-300">
                    {r.condition_expression}
                  </span>
                  <span className="text-zinc-600">→</span>
                  <span className="px-1.5 py-0.2 rounded bg-zinc-900 border border-zinc-800 text-zinc-300">
                    {r.action_type}
                  </span>
                </div>
                <div className="text-[10px] text-zinc-500 mt-1.5">
                  Completed {r.execution_count} Temporal execution{r.execution_count === 1 ? '' : 's'}
                </div>
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => toggleActive(r)}
                className={`p-1.5 rounded-lg border text-xs transition-colors ${
                  r.is_active
                    ? 'bg-zinc-900 text-emerald-400 border-zinc-800 hover:bg-zinc-850'
                    : 'bg-zinc-900 text-zinc-500 border-zinc-800 hover:text-zinc-300'
                }`}
                title={r.is_active ? 'Rule is Active' : 'Rule is Disabled'}
              >
                <Power className="w-3.5 h-3.5" />
              </button>

              <button
                onClick={() => deleteRule(r.id)}
                className="p-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-850 text-zinc-400 hover:text-red-400 border border-zinc-800 transition-colors"
                title="Delete Rule"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>

      {nextCursor && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => void loadRules(nextCursor)}
            disabled={isLoadingMore}
            className="px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            {isLoadingMore ? 'Loading...' : 'Load more automations'}
          </button>
        </div>
      )}
    </div>
  );
}

export default function AutomationsPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Automations...</div>}>
      <AutomationsContent />
    </Suspense>
  );
}
