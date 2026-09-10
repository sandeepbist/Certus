'use client';

import React, { Suspense, useCallback, useEffect, useState } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { gatewayFetch } from '@/lib/gateway-client';
import { useRealtimeEvents } from '@/hooks/useRealtimeEvents';
import { PdfEvidenceViewer, type PdfGlyphSelector } from '@/components/documents/PdfEvidenceViewer';
import { AlertCircle, ArrowLeft, Check, Copy, Download, RefreshCw, RotateCw, Trash2 } from 'lucide-react';

interface ChunkItem {
  id: string;
  chunk_index: number;
  token_count: number;
  section_title: string;
  page_number?: number;
  content: string;
  contextualized_content: string;
  start_char?: number | null;
  end_char?: number | null;
  text_locator_status: 'exact' | 'unavailable';
  text_locator_profile: string;
  text_locator_unavailable_reason?: string | null;
}

interface DocumentDetail {
  id: string;
  title: string;
  source_type: string;
  mime_type: string;
  file_size_bytes: number;
  chunk_count: number;
  processing_total_chunks: number;
  entity_count: number;
  content_hash: string;
  tags: string[];
  status: 'processing' | 'ready' | 'error';
  error_message?: string | null;
  metadata?: { chunk_strategy?: string };
  created_at: string;
  document_version_id: string;
  version_number: number;
  source_time?: string | null;
  source_time_origin: 'unspecified' | 'user_provided';
  recorded_at: string;
  is_current_version: boolean;
  embedding_profile: string;
  parsed_artifact_id: string;
  pending_derivation_id?: string | null;
  original_status: 'available' | 'unavailable' | 'missing' | 'delete_pending' | 'deleted' | 'error';
  original_filename: string;
  original_last_verified_at?: string | null;
}

interface DocumentVersionSummary {
  document_version_id: string;
  version_number: number;
  title: string;
  content_hash: string;
  status: 'processing' | 'ready' | 'error';
  source_time?: string | null;
  recorded_at: string;
  is_current_version: boolean;
  pending_derivation_id?: string | null;
  original_status: DocumentDetail['original_status'];
}

interface ChunkPage {
  limit: number;
  offset: number;
  total_count: number;
}

interface DocumentEntity {
  name: string;
  type: string;
  mention_count: number;
}

interface DocumentDetailResponse {
  document?: DocumentDetail;
  chunks?: ChunkItem[];
  entities?: DocumentEntity[];
  entities_status?: 'ready' | 'degraded';
  versions?: DocumentVersionSummary[];
  version_count?: number;
  history_truncated?: boolean;
  chunk_page?: ChunkPage;
  detail?: string;
  message?: string;
}

interface TextPositionSelector {
  type: 'TextPositionSelector';
  start: number;
  end: number;
  unit: 'unicodeCodePoint';
}

interface TextQuoteSelector {
  type: 'TextQuoteSelector';
  exact: string;
  prefix: string;
  suffix: string;
}

interface EvidenceEnvelope {
  schema_version: number;
  resolution_status: 'verified' | 'unavailable';
  unavailable_reason?: string | null;
  evidence_handle: string;
  support_scope: 'retrieved_context_not_claim_aligned';
  document: {
    id: string;
    title: string;
    version_id: string;
    version_number: number;
    is_current_version: boolean;
  };
  lineage: {
    derivation_id: string;
    parsed_artifact_id: string;
    source_object_id: string;
    layout_artifact_id?: string | null;
    text_locator_status: 'exact' | 'unavailable';
    text_locator_profile: string;
  };
  text_target?: {
    source: string;
    state: { type: 'certus:DigestState'; sha256: string };
    selector: [TextPositionSelector, TextQuoteSelector];
    quote_sha256: string;
  } | null;
  visual_target: {
    status: 'verified' | 'unavailable' | 'not_applicable';
    reason?: string;
    source?: string;
    state?: { type: 'certus:DigestState'; sha256: string };
    selector?: PdfGlyphSelector;
  };
  source_object: {
    id: string;
    status: DocumentDetail['original_status'];
    filename: string;
    mime_type: string;
    byte_length: number;
    sha256: string;
    download_available: boolean;
  };
  display: { page_number_hint?: number | null; section_title: string };
}

function responseErrorMessage(value: unknown, fallback: string) {
  if (!value || typeof value !== 'object') return fallback;
  const response = value as { detail?: unknown; message?: unknown };
  if (typeof response.message === 'string') return response.message;
  if (typeof response.detail === 'string') return response.detail;
  if (response.detail && typeof response.detail === 'object') {
    const detail = response.detail as { message?: unknown };
    if (typeof detail.message === 'string') return detail.message;
  }
  return fallback;
}

function formatSourceDate(value: string) {
  return new Date(value).toLocaleDateString(undefined, { timeZone: 'UTC' });
}

function DocumentDetailContent() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const docId = params?.id as string;
  const versionParam = searchParams.get('version');
  const evidenceChunkId = searchParams.get('chunk');
  const requestedVersion = Number(versionParam);
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [chunks, setChunks] = useState<ChunkItem[]>([]);
  const [entities, setEntities] = useState<DocumentEntity[]>([]);
  const [entitiesStatus, setEntitiesStatus] = useState<'ready' | 'degraded'>('ready');
  const [versions, setVersions] = useState<DocumentVersionSummary[]>([]);
  const [versionCount, setVersionCount] = useState(0);
  const [historyTruncated, setHistoryTruncated] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(
    Number.isInteger(requestedVersion) && requestedVersion > 0 ? requestedVersion : null,
  );
  const [chunkOffset, setChunkOffset] = useState(0);
  const [chunkPage, setChunkPage] = useState<ChunkPage>({ limit: 100, offset: 0, total_count: 0 });
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [filterQuery, setFilterQuery] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [chunkStrategy, setChunkStrategy] = useState<'token' | 'sentence' | 'recursive'>('token');
  const [isRechunking, setIsRechunking] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<EvidenceEnvelope | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [isEvidenceLoading, setIsEvidenceLoading] = useState(false);

  const refreshDocument = useCallback(() => {
    setRefreshKey((value) => value + 1);
  }, []);

  useEffect(() => {
    const nextVersion = Number.isInteger(requestedVersion) && requestedVersion > 0
      ? requestedVersion
      : null;
    setSelectedVersion(nextVersion);
    setChunkOffset(0);
  }, [requestedVersion, versionParam]);

  useRealtimeEvents(
    docId ? [`document:${docId}`] : [],
    refreshDocument,
    refreshDocument,
  );

  useEffect(() => {
    const controller = new AbortController();
    async function fetchDocDetails() {
      setLoadError(null);
      try {
        const query = new URLSearchParams({
          chunk_limit: '100',
          chunk_offset: String(chunkOffset),
        });
        if (selectedVersion !== null) query.set('version', String(selectedVersion));
        const response = await gatewayFetch(`/documents/${encodeURIComponent(docId)}?${query.toString()}`, {
          signal: controller.signal,
        });
        const data: DocumentDetailResponse = await response.json();
        if (!response.ok || !data.document) {
          throw new Error(data.detail || data.message || 'Document could not be loaded.');
        }
        if (controller.signal.aborted) return;
        setDoc(data.document);
        setChunks(data.chunks || []);
        setEntities(data.entities || []);
        setEntitiesStatus(data.entities_status || 'degraded');
        setVersions(data.versions || []);
        setVersionCount(data.version_count || 0);
        setHistoryTruncated(Boolean(data.history_truncated));
        setChunkPage(data.chunk_page || { limit: 100, offset: 0, total_count: 0 });
        const storedStrategy = data.document.metadata?.chunk_strategy;
        if (storedStrategy === 'sentence' || storedStrategy === 'semantic') {
          setChunkStrategy('sentence');
        } else if (storedStrategy === 'token' || storedStrategy === 'recursive') {
          setChunkStrategy(storedStrategy);
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setLoadError(error instanceof Error ? error.message : 'Document could not be loaded.');
      } finally {
        if (!controller.signal.aborted) setIsLoading(false);
      }
    }
    if (docId) {
      void fetchDocDetails();
    }
    return () => controller.abort();
  }, [chunkOffset, docId, refreshKey, selectedVersion]);

  useEffect(() => {
    const controller = new AbortController();
    async function fetchEvidence() {
      if (!docId || !evidenceChunkId) {
        setEvidence(null);
        setEvidenceError(null);
        setIsEvidenceLoading(false);
        return;
      }
      setIsEvidenceLoading(true);
      setEvidence(null);
      setEvidenceError(null);
      try {
        const response = await gatewayFetch(
          `/documents/${encodeURIComponent(docId)}/evidence/${encodeURIComponent(evidenceChunkId)}`,
          { signal: controller.signal },
        );
        const data: unknown = await response.json();
        if (!response.ok) {
          throw new Error(responseErrorMessage(data, 'Evidence could not be resolved.'));
        }
        if (!controller.signal.aborted) setEvidence(data as EvidenceEnvelope);
      } catch (error) {
        if (!controller.signal.aborted) {
          setEvidenceError(error instanceof Error ? error.message : 'Evidence could not be resolved.');
        }
      } finally {
        if (!controller.signal.aborted) setIsEvidenceLoading(false);
      }
    }
    void fetchEvidence();
    return () => controller.abort();
  }, [docId, evidenceChunkId]);

  const handleCopy = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const filteredChunks = chunks.filter((c) =>
    c.content.toLowerCase().includes(filterQuery.toLowerCase()) ||
    (c.section_title || '').toLowerCase().includes(filterQuery.toLowerCase())
  );

  const handleRechunk = async () => {
    if (!doc || !doc.is_current_version || doc.pending_derivation_id) return;
    setIsRechunking(true);
    setActionMessage(null);
    try {
      const response = await gatewayFetch(`/documents/${encodeURIComponent(doc.id)}/rechunk`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ strategy: chunkStrategy }),
      }, { profile: 'processing' });
      const data: { detail?: string; message?: string } = await response.json();
      if (!response.ok) throw new Error(data.detail || data.message || 'Document could not be re-chunked.');
      setActionMessage(data.message || 'Document was queued for re-embedding.');
      setRefreshKey((value) => value + 1);
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : 'Document could not be re-chunked.');
    } finally {
      setIsRechunking(false);
    }
  };

  const handleDelete = async () => {
    if (!doc || !window.confirm(`Delete “${doc.title}” from this workspace?`)) return;
    setIsDeleting(true);
    setActionMessage(null);
    try {
      const response = await gatewayFetch(`/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' });
      const data: { detail?: string; message?: string } = await response.json();
      if (!response.ok) throw new Error(data.detail || data.message || 'Document could not be deleted.');
      router.replace('/documents');
      router.refresh();
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : 'Document could not be deleted.');
      setIsDeleting(false);
    }
  };

  if (isLoading) {
    return <div className="p-8 text-center text-xs text-zinc-500">Loading document...</div>;
  }

  if (!doc) {
    return (
      <div className="p-6 max-w-3xl mx-auto space-y-4">
        <Link href="/documents" className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-white">
          <ArrowLeft className="w-3 h-3" /> Back to Documents
        </Link>
        <div className="rounded-xl border border-red-900/60 bg-red-950/30 p-5 text-sm text-red-300">
          <div className="flex items-center gap-2"><AlertCircle className="h-4 w-4" />{loadError || 'Document not found.'}</div>
          <button
            type="button"
            onClick={() => {
              setIsLoading(true);
              setRefreshKey((value) => value + 1);
            }}
            className="mt-3 inline-flex items-center gap-1 text-xs text-red-200 hover:text-white"
          >
            <RefreshCw className="h-3 w-3" /> Retry
          </button>
        </div>
      </div>
    );
  }

  const statusLabel = doc.pending_derivation_id
    ? 'Indexed · rebuilding'
    : doc.status === 'ready' ? 'Indexed' : doc.status === 'processing' ? 'Embedding' : 'Failed';
  const statusColor = doc.status === 'ready' ? 'text-emerald-400' : doc.status === 'processing' ? 'text-amber-400' : 'text-red-400';
  const processingProgress = doc.status === 'ready'
    ? 100
    : doc.processing_total_chunks > 0
      ? Math.min(99, Math.floor((doc.chunk_count / doc.processing_total_chunks) * 100))
      : 0;
  const exactTextTarget = evidence?.resolution_status === 'verified'
    ? evidence.text_target
    : null;
  const positionSelector = exactTextTarget?.selector[0];
  const quoteSelector = exactTextTarget?.selector[1];

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      {/* Top Breadcrumb */}
      <div>
        <Link
          href="/documents"
          className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-white mb-2 transition-colors"
        >
          <ArrowLeft className="w-3 h-3" />
          Back to Documents
        </Link>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold text-white tracking-tight">
              {doc.title}
            </h1>
            <p className="text-xs text-zinc-400 mt-0.5 font-mono">
              ID: {docId} • Version {doc.version_number} • SHA-256: {doc.content_hash.slice(0, 16)}...
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={doc.version_number}
              onChange={(event) => {
                const nextVersion = Number(event.target.value);
                setSelectedVersion(nextVersion);
                setChunkOffset(0);
                setIsLoading(true);
                router.replace(
                  `/documents/${encodeURIComponent(docId)}?version=${nextVersion}`,
                  { scroll: false },
                );
              }}
              aria-label="Document version"
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300"
            >
              {versions.map((item) => (
                <option key={item.document_version_id} value={item.version_number}>
                  Version {item.version_number}{item.is_current_version ? ' (current)' : ''}
                  {item.original_status === 'available' ? '' : ' · original unavailable'}
                </option>
              ))}
            </select>
            <span className={`px-2.5 py-1 rounded-md bg-zinc-900 border border-zinc-800 text-xs font-mono ${statusColor}`}>
              {statusLabel}
            </span>
            {doc.original_status === 'available' && (
              <a
                href={`/api/gateway/documents/${encodeURIComponent(doc.id)}/original?version=${doc.version_number}`}
                download
                className="inline-flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
              >
                <Download className="h-3 w-3" /> Exact original
              </a>
            )}
            <button
              type="button"
              onClick={() => setRefreshKey((value) => value + 1)}
              disabled={isRechunking || isDeleting}
              className="inline-flex items-center gap-1 rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            >
              <RefreshCw className="h-3 w-3" /> Refresh
            </button>
            <select
              value={chunkStrategy}
              onChange={(event) => setChunkStrategy(event.target.value as typeof chunkStrategy)}
              disabled={isRechunking || isDeleting || !doc.is_current_version || Boolean(doc.pending_derivation_id)}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300"
            >
              <option value="token">Token chunks</option>
              <option value="sentence">Sentence-boundary chunks</option>
              <option value="recursive">Recursive chunks</option>
            </select>
            <button
              type="button"
              onClick={handleRechunk}
              disabled={isRechunking || isDeleting || !doc.is_current_version || Boolean(doc.pending_derivation_id)}
              className="inline-flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
            >
              <RotateCw className={`h-3 w-3 ${isRechunking ? 'animate-spin' : ''}`} />
              {isRechunking ? 'Queueing...' : 'Re-chunk'}
            </button>
            <button
              type="button"
              onClick={handleDelete}
              disabled={isDeleting || isRechunking}
              className="inline-flex items-center gap-1 rounded-md border border-red-900/60 bg-red-950/30 px-2.5 py-1 text-xs text-red-300 hover:bg-red-950/60 disabled:opacity-40"
            >
              <Trash2 className="h-3 w-3" />
              {isDeleting ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </div>
      </div>

      {actionMessage && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-300">
          {actionMessage}
        </div>
      )}

      {evidenceChunkId && (
        <section className="rounded-xl border border-blue-900/60 bg-blue-950/15 p-4 text-xs">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="font-medium text-blue-100">Resolved retrieved evidence</h2>
              <p className="mt-1 text-[11px] text-blue-300/80">
                This verifies the retrieved passage and immutable lineage. It does not yet assert claim-level alignment to every sentence in the answer.
              </p>
            </div>
            <Link
              href={`/documents/${encodeURIComponent(doc.id)}?version=${doc.version_number}`}
              className="shrink-0 text-[11px] text-zinc-400 hover:text-white"
            >
              Close
            </Link>
          </div>

          {isEvidenceLoading && (
            <p className="mt-4 text-zinc-400">Verifying selector and lineage…</p>
          )}
          {evidenceError && (
            <div role="alert" className="mt-4 rounded-lg border border-red-900/60 bg-red-950/30 p-3 text-red-300">
              {evidenceError}
            </div>
          )}
          {evidence?.resolution_status === 'unavailable' && (
            <div className="mt-4 rounded-lg border border-amber-900/50 bg-amber-950/20 p-3 text-amber-200">
              Exact text position unavailable: {evidence.unavailable_reason || 'This legacy chunk predates exact locator provenance.'}
            </div>
          )}
          {evidence && exactTextTarget && positionSelector && quoteSelector && (
            <div className="mt-4 space-y-3">
              <blockquote className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-sm leading-relaxed text-zinc-200">
                {quoteSelector.exact}
              </blockquote>
              <div className="grid gap-2 text-[11px] text-zinc-400 sm:grid-cols-2">
                <p>
                  <span className="text-zinc-500">Exact span:</span>{' '}
                  <span className="font-mono">[{positionSelector.start}, {positionSelector.end}) Unicode code points</span>
                </p>
                <p>
                  <span className="text-zinc-500">Quote SHA-256:</span>{' '}
                  <span className="break-all font-mono">{exactTextTarget.quote_sha256}</span>
                </p>
                <p>
                  <span className="text-zinc-500">Parsed artifact:</span>{' '}
                  <span className="break-all font-mono">{evidence.lineage.parsed_artifact_id}</span>
                </p>
                <p>
                  <span className="text-zinc-500">Parsed SHA-256:</span>{' '}
                  <span className="break-all font-mono">{exactTextTarget.state.sha256}</span>
                </p>
                <p>
                  <span className="text-zinc-500">Original:</span>{' '}
                  {evidence.source_object.filename} · {evidence.source_object.byte_length.toLocaleString()} bytes
                </p>
                <p>
                  <span className="text-zinc-500">Original SHA-256:</span>{' '}
                  <span className="break-all font-mono">{evidence.source_object.sha256}</span>
                </p>
              </div>
              {evidence.visual_target.status === 'verified' && evidence.visual_target.selector ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-400">
                    <span>Native PDF glyph geometry verified against the immutable layout artifact.</span>
                    <span className="break-all font-mono">Layout SHA-256: {evidence.visual_target.state?.sha256}</span>
                  </div>
                  <PdfEvidenceViewer
                    sourceUrl={`/api/gateway/documents/${encodeURIComponent(evidence.document.id)}/original?version=${evidence.document.version_number}&disposition=inline`}
                    selector={evidence.visual_target.selector}
                  />
                </div>
              ) : (
                <p className="text-[11px] text-zinc-500">
                  {evidence.visual_target.reason}
                </p>
              )}
              {evidence.source_object.download_available && (
                <a
                  href={`/api/gateway/documents/${encodeURIComponent(evidence.document.id)}/original?version=${evidence.document.version_number}`}
                  download
                  className="inline-flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-[11px] text-zinc-200 hover:bg-zinc-800"
                >
                  <Download className="h-3 w-3" /> Download exact cited original
                </a>
              )}
            </div>
          )}
        </section>
      )}

      {!doc.is_current_version && (
        <div className="rounded-lg border border-blue-900/50 bg-blue-950/20 px-3 py-2 text-xs text-blue-200">
          You are inspecting retained version {doc.version_number}. Re-chunking is available only on the current version.
        </div>
      )}

      {doc.is_current_version && doc.pending_derivation_id && (
        <div className="rounded-lg border border-amber-900/40 bg-amber-950/20 px-3 py-2 text-xs text-amber-200">
          A replacement chunk/embedding derivation is building in the background. The prior indexed evidence remains searchable until the new derivation is ready.
        </div>
      )}

      <div className="rounded-xl border border-zinc-800/80 bg-zinc-950 p-4 text-xs text-zinc-400">
        <div className="grid gap-2 sm:grid-cols-2">
          <p><span className="text-zinc-500">Recorded:</span> {new Date(doc.recorded_at).toLocaleString()}</p>
          <p><span className="text-zinc-500">Source effective date:</span> {doc.source_time ? formatSourceDate(doc.source_time) : 'Unspecified'}</p>
          <p><span className="text-zinc-500">Embedding space:</span> <span className="font-mono text-[10px]">{doc.embedding_profile}</span></p>
          <p><span className="text-zinc-500">History:</span> {versionCount} retained version{versionCount === 1 ? '' : 's'}{historyTruncated ? ' (latest 100 shown)' : ''}</p>
          <p>
            <span className="text-zinc-500">Exact original:</span>{' '}
            {doc.original_status === 'available'
              ? `${doc.original_filename} · verified on download`
              : doc.original_status === 'unavailable'
                ? 'Not retained for this legacy version'
                : `Unavailable (${doc.original_status})`}
          </p>
        </div>
      </div>

      {doc.error_message && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {doc.error_message}
        </div>
      )}

      {loadError && (
        <div role="alert" className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          Live document refresh failed: {loadError}
        </div>
      )}

      {doc.status === 'processing' && (
        <div className="space-y-1.5 rounded-lg border border-amber-900/40 bg-amber-950/20 px-3 py-2.5">
          <div className="flex items-center justify-between text-[11px] text-amber-300">
            <span>Embedding document chunks</span>
            <span className="font-mono">{doc.chunk_count}/{doc.processing_total_chunks || '—'} · {processingProgress}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
            <div className="h-full rounded-full bg-amber-400 transition-[width] duration-300" style={{ width: `${processingProgress}%` }} />
          </div>
        </div>
      )}

      {/* Metadata Overview Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="p-3.5 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">
            {doc.status === 'processing' ? 'Embedded Chunks' : 'Total Chunks'}
          </span>
          <div className="text-xl font-semibold text-white mt-1">{doc.chunk_count}</div>
        </div>
        <div className="p-3.5 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Loaded Tokens</span>
          <div className="text-xl font-semibold text-white mt-1">
            {chunks.reduce((acc, c) => acc + (c.token_count || 0), 0)}
          </div>
        </div>
        <div className="p-3.5 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Format</span>
          <div className="text-xl font-semibold text-white mt-1 uppercase">{doc.source_type}</div>
        </div>
        <div className="p-3.5 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Index Type</span>
          <div className="text-xl font-semibold text-emerald-400 mt-1">HNSW</div>
        </div>
      </div>

      <div className="space-y-3">
        <h2 className="text-xs font-medium text-white">
          {doc.is_current_version ? 'Extracted Entities' : 'Current Graph Projection'} ({entities.length})
        </h2>
        <div className="flex flex-wrap gap-2 rounded-xl border border-zinc-800/80 bg-zinc-950 p-4">
          {entitiesStatus === 'degraded' ? (
            <span className="text-xs text-amber-400">Graph entities are temporarily unavailable.</span>
          ) : entities.length === 0 ? (
            <span className="text-xs text-zinc-500">No graph entities were extracted for this document.</span>
          ) : entities.map((entity) => (
            <Link
              key={`${entity.type}:${entity.name}`}
              href={`/graph?entity=${encodeURIComponent(entity.name)}`}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-300 hover:border-zinc-600 hover:text-white"
            >
              {entity.name} <span className="text-zinc-500">· {entity.type.toLowerCase()} · {entity.mention_count}</span>
            </Link>
          ))}
        </div>
      </div>

      {/* Chunks Explorer Section */}
      <div className="space-y-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <h2 className="text-xs font-medium text-white">
            Extracted Chunks ({chunkPage.total_count === 0
              ? '0'
              : `${chunkPage.offset + 1}-${Math.min(chunkPage.offset + chunks.length, chunkPage.total_count)} of ${chunkPage.total_count}`})
          </h2>
          <input
            type="text"
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            placeholder="Filter chunk text..."
            className="px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600 w-60"
          />
        </div>

        {chunkPage.total_count > chunkPage.limit && (
          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={chunkPage.offset === 0}
              onClick={() => {
                setChunkOffset(Math.max(0, chunkPage.offset - chunkPage.limit));
                setIsLoading(true);
              }}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-300 disabled:opacity-40"
            >
              Previous chunks
            </button>
            <button
              type="button"
              disabled={chunkPage.offset + chunkPage.limit >= chunkPage.total_count}
              onClick={() => {
                setChunkOffset(chunkPage.offset + chunkPage.limit);
                setIsLoading(true);
              }}
              className="rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-300 disabled:opacity-40"
            >
              Next chunks
            </button>
          </div>
        )}

        <div className="space-y-3">
          {filteredChunks.length === 0 && (
            <div className="rounded-xl border border-dashed border-zinc-800 p-6 text-center text-xs text-zinc-500">
              {doc.status === 'processing'
                ? 'Chunks are being embedded. Refresh shortly to inspect them.'
                : chunks.length === 0
                  ? 'No chunks are available for this document.'
                  : 'No chunks match the current filter.'}
            </div>
          )}
          {filteredChunks.map((chunk) => (
            <div
              key={chunk.id || chunk.chunk_index}
              className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-2.5"
            >
              <div className="flex items-center justify-between text-xs pb-2 border-b border-zinc-850">
                <div className="flex items-center gap-2">
                  <span className="px-1.5 py-0.2 rounded bg-zinc-900 text-zinc-300 font-mono text-[10px] border border-zinc-800">
                    Chunk #{chunk.chunk_index + 1}
                  </span>
                  {chunk.section_title && (
                    <span className="text-zinc-200 font-medium">{chunk.section_title}</span>
                  )}
                  {chunk.page_number && (
                    <span className="text-[11px] text-zinc-500">p.{chunk.page_number}</span>
                  )}
                  <span className={`text-[10px] ${chunk.text_locator_status === 'exact' ? 'text-emerald-400' : 'text-amber-400'}`}>
                    {chunk.text_locator_status === 'exact' ? 'exact text span' : 'legacy span unavailable'}
                  </span>
                </div>
                <div className="flex items-center gap-2.5">
                  <span className="text-zinc-500 font-mono text-[10px]">
                    {chunk.token_count} tokens
                  </span>
                  <button
                    onClick={() => handleCopy(chunk.content, String(chunk.chunk_index))}
                    className="p-0.5 hover:text-white text-zinc-500 transition-colors"
                    title="Copy"
                  >
                    {copiedId === String(chunk.chunk_index) ? (
                      <Check className="w-3 h-3 text-emerald-400" />
                    ) : (
                      <Copy className="w-3 h-3" />
                    )}
                  </button>
                </div>
              </div>

              {/* Contextual Retrieval Header */}
              <div className="p-2 rounded bg-zinc-900 border border-zinc-800 text-[11px] text-zinc-400 font-mono">
                <span className="text-zinc-300 font-semibold mr-1">Context:</span>
                {chunk.contextualized_content}
              </div>

              {/* Chunk Text */}
              <p className="text-xs text-zinc-300 leading-relaxed whitespace-pre-wrap">
                {chunk.content}
              </p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function DocumentDetailPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Document...</div>}>
      <DocumentDetailContent />
    </Suspense>
  );
}
