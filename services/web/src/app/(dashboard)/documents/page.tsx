'use client';

import React, { Suspense, useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { gatewayFetch } from '@/lib/gateway-client';
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents';
import {
  FileText,
  Upload,
  Search,
  Filter,
  CheckCircle2,
  Clock,
  AlertCircle,
  ArrowUpRight,
  RefreshCw,
} from 'lucide-react';

interface DocumentItem {
  id: string;
  title: string;
  source_type: string;
  mime_type: string;
  file_size_bytes: number;
  chunk_count: number;
  processing_total_chunks: number;
  entity_count: number;
  tags: string[];
  status: 'ready' | 'processing' | 'error';
  created_at: string;
}

function DocumentsContent() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedFormat, setSelectedFormat] = useState('all');
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const refreshDocuments = useCallback(() => {
    setRefreshKey((value) => value + 1);
  }, []);

  useRealtimeEvents(
    documents
      .filter((document) => document.status === 'processing')
      .map((document) => `document:${document.id}`),
    (event) => {
      if (event.type !== 'document.status') return;
      const documentId = event.data.document_id;
      const status = event.data.status;
      if (typeof documentId !== 'string' || !['processing', 'ready', 'error'].includes(String(status))) return;
      setDocuments((previous) => previous.map((document) => (
        document.id === documentId
          ? {
              ...document,
              status: status as DocumentItem['status'],
              chunk_count: typeof event.data.chunk_count === 'number'
                ? event.data.chunk_count
                : document.chunk_count,
              processing_total_chunks: typeof event.data.total_chunks === 'number'
                ? event.data.total_chunks
                : document.processing_total_chunks,
              entity_count: typeof event.data.entity_count === 'number'
                ? event.data.entity_count
                : document.entity_count,
            }
          : document
      )));
    },
    refreshDocuments,
  );

  const loadDocuments = useCallback(async (pageCursor?: string, signal?: AbortSignal) => {
      if (pageCursor) setIsLoadingMore(true);
      else setIsLoading(true);
      setLoadError(null);
      try {
        const query = new URLSearchParams({ limit: '50' });
        if (searchQuery.trim()) query.set('search', searchQuery.trim());
        if (selectedFormat !== 'all') query.set('source_type', selectedFormat);
        if (pageCursor) query.set('cursor', pageCursor);
        const response = await gatewayFetch(`/documents?${query}`, { signal });
        const data: {
          documents?: DocumentItem[];
          pagination?: { next_cursor?: string | null };
          detail?: string;
          message?: string;
        } = await response.json();
        if (!response.ok) {
          throw new Error(data.detail || data.message || 'Documents could not be loaded.');
        }
        const page = data.documents || [];
        setDocuments((current) => {
          if (!pageCursor) return page;
          const existingIds = new Set(current.map((document) => document.id));
          return [...current, ...page.filter((document) => !existingIds.has(document.id))];
        });
        setNextCursor(data.pagination?.next_cursor || null);
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        if (!pageCursor) setDocuments([]);
        setLoadError(error instanceof Error ? error.message : 'Documents could not be loaded.');
      } finally {
        if (pageCursor) setIsLoadingMore(false);
        else setIsLoading(false);
      }
  }, [searchQuery, selectedFormat]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadDocuments(undefined, controller.signal);
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadDocuments, refreshKey]);

  const formatBytes = (bytes: number) => {
    if (!bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">
            Documents Hub
          </h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            Manage ingested files, contextual chunks, and knowledge graph entities.
          </p>
        </div>

        <Link
          href="/documents/upload"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black hover:bg-zinc-200 text-xs font-medium transition-colors shrink-0"
        >
          <Upload className="w-3.5 h-3.5" />
          <span>Upload Document</span>
        </Link>
      </div>

      {loadError && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          <span className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {loadError}
          </span>
          <button
            type="button"
            onClick={() => {
              if (documents.length === 0) setIsLoading(true);
              setRefreshKey((value) => value + 1);
            }}
            className="inline-flex items-center gap-1 text-red-200 hover:text-white"
          >
            <RefreshCw className="h-3 w-3" />
            Retry
          </button>
        </div>
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Loaded Documents</span>
          <div className="text-xl font-semibold text-white mt-1">{documents.length}</div>
        </div>
        <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Loaded Chunks</span>
          <div className="text-xl font-semibold text-zinc-100 mt-1">
            {documents.reduce((acc, d) => acc + (d.chunk_count || 0), 0)}
          </div>
        </div>
        <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Loaded Entities</span>
          <div className="text-xl font-semibold text-zinc-100 mt-1">
            {documents.reduce((acc, d) => acc + (d.entity_count || 0), 0)}
          </div>
        </div>
      </div>

      {/* Search & Filter Bar */}
      <div className="flex flex-col sm:flex-row gap-2.5">
        <div className="relative flex-1">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search documents by title or tag..."
            className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600 transition-colors"
          />
        </div>

        <div className="flex items-center gap-1.5">
          <Filter className="w-3.5 h-3.5 text-zinc-500" />
          <select
            value={selectedFormat}
            onChange={(e) => setSelectedFormat(e.target.value)}
            className="bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 rounded-lg px-2.5 py-2 focus:outline-none cursor-pointer"
          >
            <option value="all">All Formats</option>
            <option value="pdf">PDF</option>
            <option value="markdown">Markdown</option>
            <option value="docx">Word DOCX</option>
            <option value="text">Text</option>
          </select>
        </div>
      </div>

      {/* Document Table */}
      <div className="rounded-xl border border-zinc-800/80 bg-zinc-950 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-zinc-900/80 text-zinc-400 uppercase tracking-wider text-[10px] border-b border-zinc-800">
              <tr>
                <th className="p-3 pl-4 font-medium">Title</th>
                <th className="p-3 font-medium">Format</th>
                <th className="p-3 font-medium">Size</th>
                <th className="p-3 font-medium">Chunks</th>
                <th className="p-3 font-medium">Entities</th>
                <th className="p-3 font-medium">Tags</th>
                <th className="p-3 font-medium">Status</th>
                <th className="p-3 pr-4 text-right font-medium">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-850 text-zinc-300">
              {isLoading ? (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-zinc-500">
                    Loading workspace documents...
                  </td>
                </tr>
              ) : documents.length === 0 ? (
                <tr>
                  <td colSpan={8} className="p-6 text-center text-zinc-500">
                    {loadError
                      ? 'Documents are unavailable until the service recovers.'
                      : searchQuery.trim() || selectedFormat !== 'all'
                        ? 'No documents match the current filters.'
                        : 'No documents yet. Upload a file to build your knowledge base.'}
                  </td>
                </tr>
              ) : (
                documents.map((doc) => (
                  <tr key={doc.id} className="hover:bg-zinc-900/50 transition-colors">
                    <td className="p-3 pl-4 font-medium text-zinc-100 flex items-center gap-2">
                      <FileText className="w-3.5 h-3.5 text-zinc-400 shrink-0" />
                      <span className="truncate max-w-[200px]">{doc.title}</span>
                    </td>
                    <td className="p-3">
                      <span className="uppercase text-[10px] px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 font-mono border border-zinc-800">
                        {doc.source_type}
                      </span>
                    </td>
                    <td className="p-3 text-zinc-400 font-mono text-[11px]">
                      {formatBytes(doc.file_size_bytes)}
                    </td>
                    <td className="p-3 font-medium text-zinc-200 font-mono">
                      {doc.chunk_count || 0}
                    </td>
                    <td className="p-3 font-medium text-zinc-200 font-mono">
                      {doc.entity_count || 0}
                    </td>
                    <td className="p-3">
                      <div className="flex flex-wrap gap-1">
                        {doc.tags && doc.tags.length > 0 ? (
                          doc.tags.map((t, idx) => (
                            <span
                              key={idx}
                              className="px-1.5 py-0.5 rounded bg-zinc-900 text-[10px] text-zinc-400 border border-zinc-800"
                            >
                              #{t}
                            </span>
                          ))
                        ) : (
                          <span className="text-zinc-600 text-[10px]">—</span>
                        )}
                      </div>
                    </td>
                    <td className="p-3">
                      {doc.status === 'ready' ? (
                        <span className="inline-flex items-center gap-1 text-[11px] text-emerald-400 font-medium">
                          <CheckCircle2 className="w-3 h-3" />
                          Ready
                        </span>
                      ) : doc.status === 'processing' ? (
                        <span className="inline-flex items-center gap-1 text-[11px] text-amber-400 font-medium">
                          <Clock className="w-3 h-3" />
                          Processing {doc.processing_total_chunks > 0
                            ? `${Math.min(99, Math.floor((doc.chunk_count / doc.processing_total_chunks) * 100))}%`
                            : ''}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[11px] text-red-400 font-medium">
                          <AlertCircle className="w-3 h-3" />
                          Error
                        </span>
                      )}
                    </td>
                    <td className="p-3 pr-4 text-right">
                      <Link
                        href={`/documents/${doc.id}`}
                        className="inline-flex items-center gap-0.5 text-xs text-zinc-300 hover:text-white font-medium"
                      >
                        Inspect
                        <ArrowUpRight className="w-3 h-3" />
                      </Link>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {nextCursor && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => void loadDocuments(nextCursor)}
            disabled={isLoadingMore}
            className="rounded-lg border border-zinc-800 bg-zinc-950 px-4 py-2 text-xs text-zinc-300 hover:text-white disabled:opacity-40"
          >
            {isLoadingMore ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  );
}

export default function DocumentsPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Documents...</div>}>
      <DocumentsContent />
    </Suspense>
  );
}
