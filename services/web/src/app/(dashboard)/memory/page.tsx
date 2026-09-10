'use client';

import React, { useState, useEffect, useCallback, Suspense } from 'react';
import {
  Plus,
  Search,
  Trash2,
  Pin,
  RefreshCw,
} from 'lucide-react';
import { gatewayFetch } from '@/lib/gateway-client';

interface MemoryItem {
  id: string;
  fact: string;
  category: string;
  confidence: number;
  access_count: number;
  is_pinned?: boolean;
  created_at: string;
}

function MemoryContent() {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [newFact, setNewFact] = useState('');
  const [newCategory, setNewCategory] = useState('preference');
  const [showAddModal, setShowAddModal] = useState(false);
  const [isReflecting, setIsReflecting] = useState(false);
  const [reflectionNotice, setReflectionNotice] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const loadMemories = useCallback(async (pageCursor?: string, signal?: AbortSignal) => {
    if (pageCursor) setIsLoadingMore(true);
    else setIsLoading(true);
    try {
      const query = new URLSearchParams({ limit: '50' });
      if (pageCursor) query.set('cursor', pageCursor);
      const res = await gatewayFetch(`/memories?${query}`, { signal });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.detail || 'Memories could not be loaded.');
      const page = Array.isArray(data.memories) ? data.memories as MemoryItem[] : [];
      setMemories((current) => {
        if (!pageCursor) return page;
        const existingIds = new Set(current.map((memory) => memory.id));
        return [...current, ...page.filter((memory) => !existingIds.has(memory.id))];
      });
      setNextCursor(data.pagination?.next_cursor || null);
      setErrorMessage(null);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setErrorMessage(error instanceof Error ? error.message : 'Memories could not be loaded.');
    } finally {
      if (pageCursor) setIsLoadingMore(false);
      else setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadMemories(undefined, controller.signal);
    return () => controller.abort();
  }, [loadMemories]);

  const handleCreateMemory = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newFact.trim()) return;

    try {
      const res = await gatewayFetch('/memories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fact: newFact, category: newCategory }),
      });
      const data = await res.json();
      if (!res.ok) {
        const detail = data.detail;
        throw new Error(detail?.message || detail || 'Memory could not be stored.');
      }
      setMemories((prev) => [data.memory, ...prev]);
      setNewFact('');
      setShowAddModal(false);
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Memory could not be stored.');
    }
  };

  const handleDelete = async (id: string) => {
    try {
      const response = await gatewayFetch(`/memories/${id}`, { method: 'DELETE' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Memory could not be deleted.');
      await loadMemories();
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Memory could not be deleted.');
    }
  };

  const handleReflect = async () => {
    setIsReflecting(true);
    setReflectionNotice(null);
    try {
      const res = await gatewayFetch(
        '/memories/reflect',
        { method: 'POST' },
        { profile: 'processing' },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Memory reflection failed.');
      setReflectionNotice(
        `Examined ${data.memories_examined} memories and found ${data.clusters_formed} related cluster${data.clusters_formed === 1 ? '' : 's'}.`,
      );
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Memory reflection failed.');
    } finally {
      setIsReflecting(false);
    }
  };

  const filtered = memories.filter((m) =>
    m.fact.toLowerCase().includes(searchQuery.toLowerCase()) ||
    m.category.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">
            Memory
          </h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            Long-term facts, preferences, and background semantic reflection.
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={handleReflect}
            disabled={isReflecting}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 text-zinc-200 text-xs font-medium transition-colors"
          >
            <RefreshCw className={`w-3 h-3 ${isReflecting ? 'animate-spin' : ''}`} />
            <span>{isReflecting ? 'Reflecting...' : 'Reflect'}</span>
          </button>

          <button
            onClick={() => setShowAddModal(true)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black hover:bg-zinc-200 text-xs font-medium transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Add Memory</span>
          </button>
        </div>
      </div>

      {errorMessage && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {errorMessage}
        </div>
      )}

      {/* Reflection Alert Notice */}
      {reflectionNotice && (
        <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          <span>{reflectionNotice}</span>
        </div>
      )}

      {/* Search */}
      <div className="relative">
        <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search memories..."
          className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
        />
      </div>

      {/* Memory Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {isLoading && (
          <div className="md:col-span-2 p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            Loading memories...
          </div>
        )}
        {!isLoading && filtered.length === 0 && (
          <div className="md:col-span-2 p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            No active memories found.
          </div>
        )}
        {filtered.map((m) => (
          <div
            key={m.id}
            className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-3"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="px-1.5 py-0.2 rounded text-[10px] font-mono uppercase bg-zinc-900 text-zinc-300 border border-zinc-800">
                {m.category}
              </span>
              <div className="flex items-center gap-1.5">
                {m.is_pinned && <Pin className="w-3 h-3 text-zinc-400 fill-zinc-400" />}
                <button
                  onClick={() => handleDelete(m.id)}
                  className="p-1 rounded text-zinc-500 hover:text-red-400 transition-colors"
                  title="Delete"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>

            <p className="text-xs text-zinc-200 leading-relaxed">{m.fact}</p>

            <div className="flex items-center justify-between text-[10px] text-zinc-500 border-t border-zinc-850 pt-2 font-mono">
              <span>Confidence: {((m.confidence ?? 0) * 100).toFixed(0)}%</span>
              <span>Accessed {m.access_count}x</span>
            </div>
          </div>
        ))}
      </div>

      {nextCursor && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => void loadMemories(nextCursor)}
            disabled={isLoadingMore}
            className="px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            {isLoadingMore ? 'Loading...' : 'Load more memories'}
          </button>
        </div>
      )}

      {/* Add Memory Modal */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="w-full max-w-md bg-zinc-950 border border-zinc-800 rounded-xl p-5 space-y-4">
            <h2 className="text-sm font-semibold text-white">
              Store Memory Fact
            </h2>
            <form onSubmit={handleCreateMemory} className="space-y-3">
              <div>
                <label className="text-xs text-zinc-400">Fact</label>
                <textarea
                  value={newFact}
                  onChange={(e) => setNewFact(e.target.value)}
                  placeholder="e.g. User prefers JWT expiration of 7 days on development tenants"
                  className="w-full mt-1 p-2.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600 h-20 resize-none"
                  required
                />
              </div>

              <div>
                <label className="text-xs text-zinc-400">Category</label>
                <select
                  value={newCategory}
                  onChange={(e) => setNewCategory(e.target.value)}
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none cursor-pointer"
                >
                  <option value="preference">Preference</option>
                  <option value="fact">Fact</option>
                  <option value="constraint">Constraint</option>
                  <option value="goal">Goal</option>
                </select>
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowAddModal(false)}
                  className="px-3 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-300 text-xs transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-3 py-1.5 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium transition-colors"
                >
                  Save Fact
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default function MemoryPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Memory...</div>}>
      <MemoryContent />
    </Suspense>
  );
}
