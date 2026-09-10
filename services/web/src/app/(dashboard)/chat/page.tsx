'use client';

import React, {
  useState,
  useRef,
  useEffect,
  useCallback,
  Suspense,
} from 'react';
import Link from 'next/link';
import {
  Send,
  Bot,
  User,
  FileText,
  Activity,
  Copy,
  Check,
  Cpu,
  Layers,
  BookOpen,
  Fingerprint,
  Search,
  X,
} from 'lucide-react';
import {
  useStreamingChat,
  type ChatVersionScope,
  type Citation,
} from '../../../hooks/useStreamingChat';
import { gatewayFetch } from '@/lib/gateway-client';

interface ScopeDocument {
  id: string;
  title: string;
  status: 'ready';
}

function formatSourceDate(value: string) {
  return new Date(value).toLocaleDateString(undefined, { timeZone: 'UTC' });
}

function ChatContent() {
  const {
    messages,
    agentEvents,
    isStreaming,
    selectedModel,
    setSelectedModel,
    sendMessage,
    cancel,
  } = useStreamingChat();

  const [inputQuery, setInputQuery] = useState('');
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const [showTracePanel, setShowTracePanel] = useState(true);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [generativeProviderConfigured, setGenerativeProviderConfigured] = useState(false);
  const [availableModels, setAvailableModels] = useState<string[]>(['auto']);
  const [scopeDocuments, setScopeDocuments] = useState<ScopeDocument[]>([]);
  const [selectedScopeDocuments, setSelectedScopeDocuments] = useState<ScopeDocument[]>([]);
  const [versionScope, setVersionScope] = useState<ChatVersionScope>('auto');
  const [scopeSearch, setScopeSearch] = useState('');
  const [scopeNextCursor, setScopeNextCursor] = useState<string | null>(null);
  const [scopeLoading, setScopeLoading] = useState(true);
  const [scopeLoadingMore, setScopeLoadingMore] = useState(false);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const loadScopeDocuments = useCallback(async (
    pageCursor?: string,
    signal?: AbortSignal,
  ) => {
    if (pageCursor) setScopeLoadingMore(true);
    else setScopeLoading(true);
    setScopeError(null);
    try {
      const query = new URLSearchParams({ limit: '50', status: 'ready' });
      if (scopeSearch.trim()) query.set('search', scopeSearch.trim());
      if (pageCursor) query.set('cursor', pageCursor);
      const response = await gatewayFetch(`/documents?${query}`, { signal });
      const data: {
        documents?: Array<Partial<ScopeDocument> & { status?: string }>;
        pagination?: { next_cursor?: string | null };
        detail?: string;
        message?: string;
      } = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || data.message || 'Documents could not be loaded.');
      }
      const page = (data.documents || []).flatMap((document) => (
        typeof document.id === 'string'
        && typeof document.title === 'string'
        && document.status === 'ready'
          ? [{ id: document.id, title: document.title, status: 'ready' as const }]
          : []
      ));
      setScopeDocuments((current) => {
        if (!pageCursor) return page;
        const knownIds = new Set(current.map((document) => document.id));
        return [...current, ...page.filter((document) => !knownIds.has(document.id))];
      });
      setScopeNextCursor(data.pagination?.next_cursor || null);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (!pageCursor) setScopeDocuments([]);
      setScopeError(error instanceof Error ? error.message : 'Documents could not be loaded.');
    } finally {
      if (pageCursor) setScopeLoadingMore(false);
      else setScopeLoading(false);
    }
  }, [scopeSearch]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadScopeDocuments(undefined, controller.signal);
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadScopeDocuments]);

  const toggleScopeDocument = (document: ScopeDocument) => {
    setSelectedScopeDocuments((current) => {
      if (current.some((selected) => selected.id === document.id)) {
        return current.filter((selected) => selected.id !== document.id);
      }
      return current.length < 10 ? [...current, document] : current;
    });
  };

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/settings', { credentials: 'same-origin' })
      .then(async (response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (cancelled) return;
        const configured = Boolean(payload?.generative_provider_configured);
        setGenerativeProviderConfigured(configured);
        setAvailableModels(Array.isArray(payload?.available_models) ? payload.available_models : ['auto']);
        if (!configured) setSelectedModel('auto');
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [setSelectedModel]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputQuery.trim() || isStreaming) return;
    sendMessage(
      inputQuery,
      selectedScopeDocuments.map((document) => document.id),
      versionScope,
    );
    setInputQuery('');
  };

  const handleCopy = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const quickPrompts = [
    'Explain the Certus intent contract and verification pipeline',
    'How does pgvector HNSW indexing optimize hybrid search?',
    'Show me the deduplication and contextual chunking pipeline',
    'What entities are extracted into Neo4j graph database?',
  ];

  return (
    <div className="flex h-[calc(100vh-3.5rem)] overflow-hidden">
      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-black">
        {/* Chat Header Bar */}
        <div className="h-14 px-6 border-b border-zinc-800/80 flex items-center justify-between bg-zinc-950/80 backdrop-blur-md">
          <div className="flex items-center gap-2.5">
            <h1 className="text-sm font-semibold text-white flex items-center gap-2">
              Chat & Retrieval
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
            </h1>
            <span className="text-[11px] text-zinc-500">Hybrid RAG + Intent Assurance</span>
          </div>

          <div className="flex items-center gap-2.5">
            {/* Model Selector */}
            <div className="flex items-center gap-2 bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1">
              <Cpu className="w-3.5 h-3.5 text-zinc-400" />
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="bg-transparent text-xs text-zinc-200 focus:outline-none cursor-pointer"
              >
                <option value="auto">{generativeProviderConfigured ? 'Automatic model routing' : 'Local extractive mode'}</option>
                {generativeProviderConfigured && availableModels
                  .filter((model) => model !== 'auto')
                  .map((model) => <option key={model} value={model}>Prefer {model}</option>)}
              </select>
            </div>

            {/* Trace Toggle Button */}
            <button
              onClick={() => setShowTracePanel(!showTracePanel)}
              className={`p-1.5 px-2.5 rounded-lg border text-xs flex items-center gap-1.5 transition-colors ${
                showTracePanel
                  ? 'bg-zinc-800 border-zinc-700 text-white'
                  : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:text-zinc-200'
              }`}
            >
              <Activity className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Trace</span>
            </button>
          </div>
        </div>

        {/* Message Stream */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex gap-3 max-w-3xl ${
                msg.role === 'user' ? 'ml-auto flex-row-reverse' : 'mr-auto'
              }`}
            >
              {/* Avatar */}
              <div
                className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 border text-xs font-medium ${
                  msg.role === 'user'
                    ? 'bg-zinc-200 text-black border-zinc-300'
                    : 'bg-zinc-900 text-zinc-200 border-zinc-800'
                }`}
              >
                {msg.role === 'user' ? <User className="w-3.5 h-3.5" /> : <Bot className="w-3.5 h-3.5" />}
              </div>

              {/* Message Bubble */}
              <div
                className={`group relative rounded-xl p-4 text-xs leading-relaxed max-w-[85%] border ${
                  msg.role === 'user'
                    ? 'bg-zinc-100 text-black border-zinc-200 font-medium'
                    : 'bg-zinc-950 border-zinc-800 text-zinc-200'
                }`}
              >
                <div className="flex items-center justify-between gap-4 mb-2 pb-1.5 border-b border-zinc-850 text-[11px] text-zinc-400">
                  <span className="font-medium text-zinc-300">
                    {msg.role === 'user' ? 'You' : 'Certus'}
                  </span>
                  <div className="flex items-center gap-2">
                    {msg.modelUsed && (
                      <span className="px-1.5 py-0.5 rounded bg-zinc-900 text-[10px] text-zinc-400 font-mono border border-zinc-800">
                        {msg.modelUsed}
                      </span>
                    )}
                    {typeof msg.evalScore === 'number' && (
                      <span className="px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 text-[10px] font-mono flex items-center gap-1 border border-zinc-800" title="Mechanical claim/source integrity only; semantic entailment has not been evaluated">
                        <Layers className="w-2.5 h-2.5" />
                        Evidence links {msg.evalScore === 1 ? 'passed' : 'failed'}
                      </span>
                    )}
                    {msg.provenance && (
                      <span
                        className="px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 text-[10px] font-mono flex items-center gap-1 border border-zinc-800"
                        title={msg.provenance.modelRevisionLocked
                          ? 'Exact evidence, prompt, schema, validator, and model snapshot are fingerprinted for replay.'
                          : 'Evidence, prompt, schema, and validator are fingerprinted. The configured provider model is an alias rather than an immutable dated snapshot.'}
                      >
                        <Fingerprint className="w-2.5 h-2.5" />
                        Replay pack {msg.provenance.evidenceSourceCount}
                        {!msg.provenance.modelRevisionLocked && msg.provenance.provider === 'openai' ? ' · model alias' : ''}
                      </span>
                    )}
                    {msg.answerStatus && (
                      <span className="px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 text-[10px] font-mono border border-zinc-800">
                        {msg.answerStatus.replaceAll('_', ' ')}
                      </span>
                    )}
                    {msg.selectedDocumentIds && msg.selectedDocumentIds.length > 0 && (
                      <span
                        className="px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 text-[10px] font-mono border border-zinc-800"
                        title="This request was restricted to a product-selected document allow-list."
                      >
                        scope {msg.selectedDocumentIds.length}
                      </span>
                    )}
                    {msg.selectedVersionScope && msg.selectedVersionScope !== 'auto' && (
                      <span
                        className="px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 text-[10px] font-mono border border-zinc-800"
                        title="This request used an explicit product-selected document version scope."
                      >
                        {msg.selectedVersionScope === 'current_only'
                          ? 'current versions'
                          : 'all history'}
                      </span>
                    )}
                    <span>{msg.timestamp}</span>
                    <button
                      onClick={() => handleCopy(msg.content, msg.id)}
                      className="opacity-0 group-hover:opacity-100 transition-opacity p-0.5 hover:text-white"
                      title="Copy"
                    >
                      {copiedId === msg.id ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                    </button>
                  </div>
                </div>

                {/* Message Body */}
                <div className="whitespace-pre-wrap font-sans">
                  {msg.content || (
                    <span className="inline-flex items-center gap-1.5 text-zinc-500">
                      <span className="w-1.5 h-1.5 rounded-full bg-zinc-400 animate-pulse"></span>
                      Reasoning...
                    </span>
                  )}
                </div>

                {msg.claims && msg.claims.length > 0 && (
                  <div className="mt-3 pt-2.5 border-t border-zinc-850 space-y-1.5">
                    <p className="text-[11px] font-medium text-zinc-400 flex items-center gap-1">
                      <Layers className="w-3 h-3" />
                      Atomic claim ledger ({msg.claims.length})
                    </p>
                    {msg.claims.map((claim) => (
                      <div key={claim.claimId} className="rounded border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-[10px] text-zinc-400">
                        <div className="flex items-center gap-1.5 mb-0.5">
                          <span className="font-mono text-zinc-300">{claim.claimId}</span>
                          <span className="text-zinc-600">·</span>
                          <span>{claim.sourceRefs.map((source) => source.sourceId).join(', ')}</span>
                          <span className="ml-auto text-amber-300/70" title="IDs, exact values, quotes, polarity, and term coverage passed; semantic entailment was not evaluated">
                            mechanical checks only
                          </span>
                        </div>
                        <p className="line-clamp-2">{claim.text}</p>
                      </div>
                    ))}
                  </div>
                )}

                {/* Citations Row */}
                {msg.citations && msg.citations.length > 0 && (
                  <div className="mt-3 pt-2.5 border-t border-zinc-850">
                    <p className="text-[11px] font-medium text-zinc-400 mb-1.5 flex items-center gap-1">
                      <BookOpen className="w-3 h-3 text-zinc-400" />
                      Claim-selected document evidence ({msg.citations.length})
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {msg.citations.map((c, i) => (
                        <button
                          key={`${c.chunkId}:${i}`}
                          onClick={() => setActiveCitation(c)}
                          className="flex items-center gap-1.5 px-2 py-1 rounded bg-zinc-900 hover:bg-zinc-850 border border-zinc-800 text-zinc-300 text-[11px] transition-colors"
                        >
                          <FileText className="w-3 h-3 text-zinc-400" />
                          <span className="truncate max-w-[160px]">{c.documentTitle}</span>
                          {c.versionNumber && (
                            <span className="font-mono text-[9px] text-zinc-500">v{c.versionNumber}</span>
                          )}
                          {c.claimIds && c.claimIds.length > 0 && (
                            <span className="font-mono text-[9px] text-zinc-500">{c.claimIds.join(',')}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        {/* Quick Prompts Bar */}
        <div className="px-6 py-2 border-t border-zinc-850 bg-zinc-950 overflow-x-auto flex gap-2">
          {quickPrompts.map((prompt, i) => (
            <button
              key={i}
              onClick={() => sendMessage(
                prompt,
                selectedScopeDocuments.map((document) => document.id),
                versionScope,
              )}
              className="text-[11px] px-2.5 py-1 rounded-md bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-400 hover:text-zinc-200 whitespace-nowrap transition-colors"
            >
              {prompt}
            </button>
          ))}
        </div>

        {/* Input Bar */}
        <div className="p-4 px-6 border-t border-zinc-800 bg-zinc-950">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <details className="group relative">
              <summary className="list-none cursor-pointer rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-[11px] text-zinc-300 hover:border-zinc-700">
                <span className="inline-flex items-center gap-1.5">
                  <FileText className="h-3 w-3 text-zinc-500" />
                  {selectedScopeDocuments.length === 0
                    ? 'All ready documents'
                    : `${selectedScopeDocuments.length} selected document${selectedScopeDocuments.length === 1 ? '' : 's'}`}
                </span>
              </summary>
              <div className="absolute bottom-full left-0 z-30 mb-2 w-96 max-w-[calc(100vw-3rem)] rounded-lg border border-zinc-700 bg-zinc-950 p-2 shadow-2xl">
                <div className="flex items-center justify-between px-1 pb-2">
                  <div>
                    <p className="text-xs font-medium text-zinc-200">Document allow-list</p>
                    <p className="text-[10px] text-zinc-500">Select up to 10; no selection searches the workspace.</p>
                  </div>
                  {selectedScopeDocuments.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setSelectedScopeDocuments([])}
                      disabled={isStreaming}
                      className="inline-flex items-center gap-1 text-[10px] text-zinc-500 hover:text-zinc-200 disabled:opacity-40"
                    >
                      <X className="h-3 w-3" /> Clear
                    </button>
                  )}
                </div>
                <label className="mb-2 flex items-center gap-2 rounded-md border border-zinc-800 bg-zinc-900 px-2">
                  <Search className="h-3 w-3 text-zinc-500" />
                  <input
                    type="search"
                    value={scopeSearch}
                    onChange={(event) => setScopeSearch(event.target.value)}
                    placeholder="Search document titles"
                    className="w-full bg-transparent py-1.5 text-[11px] text-zinc-200 outline-none placeholder:text-zinc-600"
                  />
                </label>
                <div className="max-h-56 space-y-1 overflow-y-auto">
                  {[...selectedScopeDocuments, ...scopeDocuments.filter(
                    (document) => !selectedScopeDocuments.some((selected) => selected.id === document.id),
                  )].map((document) => {
                    const selected = selectedScopeDocuments.some((item) => item.id === document.id);
                    const limitReached = !selected && selectedScopeDocuments.length >= 10;
                    return (
                      <label
                        key={document.id}
                        className={`flex items-center gap-2 rounded px-2 py-1.5 text-[11px] ${
                          limitReached || isStreaming
                            ? 'cursor-not-allowed text-zinc-600'
                            : 'cursor-pointer text-zinc-300 hover:bg-zinc-900'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={selected}
                          disabled={limitReached || isStreaming}
                          onChange={() => toggleScopeDocument(document)}
                          className="accent-zinc-200"
                        />
                        <span className="truncate" title={document.title}>{document.title}</span>
                      </label>
                    );
                  })}
                  {scopeLoading && <p className="px-2 py-3 text-center text-[11px] text-zinc-500">Loading documents…</p>}
                  {!scopeLoading && scopeDocuments.length === 0 && !scopeError && (
                    <p className="px-2 py-3 text-center text-[11px] text-zinc-500">No ready documents found.</p>
                  )}
                  {scopeError && <p className="px-2 py-2 text-[11px] text-red-300">{scopeError}</p>}
                </div>
                {scopeNextCursor && (
                  <button
                    type="button"
                    disabled={scopeLoadingMore}
                    onClick={() => void loadScopeDocuments(scopeNextCursor)}
                    className="mt-2 w-full rounded border border-zinc-800 py-1.5 text-[10px] text-zinc-400 hover:bg-zinc-900 disabled:opacity-50"
                  >
                    {scopeLoadingMore ? 'Loading…' : 'Load more results'}
                  </button>
                )}
              </div>
            </details>
            <label className="inline-flex items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-[11px] text-zinc-300">
              <span className="text-zinc-500">Versions</span>
              <select
                value={versionScope}
                onChange={(event) => setVersionScope(event.target.value as ChatVersionScope)}
                disabled={isStreaming}
                className="bg-transparent text-[11px] text-zinc-200 outline-none disabled:opacity-50"
                title="As-of questions always retain their explicit temporal cutoff."
              >
                <option value="auto">Automatic (history by default)</option>
                <option value="all_history">All retained versions</option>
                <option value="current_only">Current versions only</option>
              </select>
            </label>
            {selectedScopeDocuments.length > 0 && (
              <span className="truncate text-[10px] text-zinc-500">
                Retrieval is restricted to {selectedScopeDocuments.map((document) => document.title).join(', ')}
              </span>
            )}
          </div>
          <form onSubmit={handleSubmit} className="relative flex items-center">
            <input
              type="text"
              id="chat-input"
              value={inputQuery}
              onChange={(e) => setInputQuery(e.target.value)}
              placeholder="Ask a question across your workspace..."
              className="w-full pl-3.5 pr-20 py-2.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
            <button
              type={isStreaming ? 'button' : 'submit'}
              onClick={isStreaming ? cancel : undefined}
              disabled={!isStreaming && !inputQuery.trim()}
              className="absolute right-1.5 px-3 py-1.5 rounded-md bg-white hover:bg-zinc-200 disabled:opacity-40 text-black text-xs font-medium flex items-center gap-1 transition-colors"
            >
              {isStreaming ? (
                <span>Stop</span>
              ) : (
                <>
                  <span>Send</span>
                  <Send className="w-3 h-3" />
                </>
              )}
            </button>
          </form>
        </div>
      </div>

      {/* Right Side: Multi-Agent Trace Panel */}
      {showTracePanel && (
        <aside className="w-72 border-l border-zinc-800 bg-zinc-950 flex flex-col shrink-0">
          <div className="p-3.5 border-b border-zinc-800 flex items-center justify-between">
            <h3 className="text-xs font-medium text-zinc-200 flex items-center gap-1.5">
              <Activity className="w-3.5 h-3.5 text-zinc-400" />
              Agent Timeline
            </h3>
            <span className="text-[10px] text-zinc-500 font-mono">LangGraph</span>
          </div>

          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {agentEvents.length === 0 ? (
              <div className="p-4 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-lg">
                <Layers className="w-5 h-5 mx-auto mb-1.5 text-zinc-600" />
                Submit a query to inspect live agent execution traces.
              </div>
            ) : (
              agentEvents.map((ev, i) => (
                <div
                  key={i}
                  className="p-2.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs space-y-1"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] font-semibold text-zinc-300 uppercase">
                      [{ev.agent}]
                    </span>
                    <span className="text-[10px] px-1 py-0.2 rounded bg-zinc-800 text-zinc-400 font-mono">
                      {ev.status}
                    </span>
                  </div>
                  <p className="text-zinc-400 text-[11px]">{ev.action}</p>
                </div>
              ))
            )}
          </div>

          {/* Selected Citation Drawer */}
          {activeCitation && (
            <div className="p-3 border-t border-zinc-800 bg-zinc-900 text-xs space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="font-medium text-zinc-200 flex items-center gap-1">
                  <FileText className="w-3 h-3 text-zinc-400" />
                  {activeCitation.documentTitle}
                </span>
                <button
                  onClick={() => setActiveCitation(null)}
                  className="text-zinc-500 hover:text-zinc-300 text-xs"
                >
                  ✕
                </button>
              </div>
              <p className="max-h-48 overflow-y-auto whitespace-pre-wrap text-zinc-400 text-[11px] bg-zinc-950 p-2 rounded border border-zinc-850">
                &quot;{activeCitation.quote}&quot;
              </p>
              <div className="space-y-0.5 text-[10px] text-zinc-500">
                <p>{activeCitation.versionNumber
                  ? `Version ${activeCitation.versionNumber}${activeCitation.isCurrentVersion ? ' (current)' : ' (retained)'}`
                  : 'Legacy citation'}{activeCitation.pageNumber ? ` · Page ${activeCitation.pageNumber}` : ''}</p>
                <p>
                  Source effective date: {activeCitation.sourceTime
                    ? formatSourceDate(activeCitation.sourceTime)
                    : 'Unspecified'}
                </p>
                {activeCitation.recordedAt && (
                  <p>Recorded: {new Date(activeCitation.recordedAt).toLocaleString()}</p>
                )}
                {activeCitation.contentHash && (
                  <p className="font-mono">SHA-256: {activeCitation.contentHash.slice(0, 16)}…</p>
                )}
                <p>
                  Text locator: {activeCitation.textLocatorStatus === 'exact'
                    ? 'Exact parsed-artifact span'
                    : 'Unavailable for this legacy chunk'}
                </p>
                {activeCitation.quoteSha256 && (
                  <p className="font-mono">Quote SHA-256: {activeCitation.quoteSha256.slice(0, 16)}…</p>
                )}
                {activeCitation.claimIds && activeCitation.claimIds.length > 0 && (
                  <p className="font-mono">Selected for: {activeCitation.claimIds.join(', ')}</p>
                )}
                {activeCitation.verificationStatus === 'mechanical_checks_passed_semantic_not_evaluated' && (
                  <p className="text-amber-300/70">Mechanical checks passed · semantic entailment not evaluated</p>
                )}
              </div>
              <Link
                href={`/documents/${encodeURIComponent(activeCitation.documentId)}?${new URLSearchParams({
                  ...(activeCitation.versionNumber
                    ? { version: String(activeCitation.versionNumber) }
                    : {}),
                  chunk: activeCitation.chunkId,
                }).toString()}`}
                className="inline-flex text-[11px] text-zinc-300 underline decoration-zinc-600 underline-offset-2 hover:text-white"
              >
                Inspect evidence
              </Link>
            </div>
          )}
        </aside>
      )}
    </div>
  );
}

export default function ChatPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Chat...</div>}>
      <ChatContent />
    </Suspense>
  );
}
