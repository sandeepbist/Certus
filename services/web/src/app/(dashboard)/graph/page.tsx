'use client';

import React, { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import {
  AlertCircle,
  Check,
  ChevronRight,
  Copy,
  FileText,
  Info,
  Maximize2,
  RefreshCw,
  Search,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { gatewayFetch } from '@/lib/gateway-client';

interface ApiGraphNode {
  id: string;
  record_id: string;
  name: string;
  type: string;
  mention_count: number;
  document_count: number;
  created_at: string;
}

interface GraphNode extends ApiGraphNode {
  x: number;
  y: number;
}

interface GraphLink {
  id: string;
  source: string;
  target: string;
  type: string;
  weight: number;
}

interface GraphResponse {
  nodes?: ApiGraphNode[];
  links?: GraphLink[];
  stats?: { nodes: number; links: number; entities: number; documents: number };
  truncated?: boolean;
  detail?: string;
  message?: string;
}

function layoutNodes(nodes: ApiGraphNode[]): GraphNode[] {
  const entities = nodes.filter((node) => node.type !== 'DOCUMENT');
  const documents = nodes.filter((node) => node.type === 'DOCUMENT');
  const positionRing = (items: ApiGraphNode[], baseRadius: number, ringSize: number) => items.map((node, index) => {
    const ring = Math.floor(index / ringSize);
    const position = index % ringSize;
    const itemsInRing = Math.min(ringSize, items.length - ring * ringSize);
    const angle = (Math.PI * 2 * position) / Math.max(itemsInRing, 1) - Math.PI / 2;
    const radius = baseRadius + ring * 85;
    return {
      ...node,
      x: 650 + Math.cos(angle) * radius,
      y: 410 + Math.sin(angle) * radius,
    };
  });

  return [
    ...positionRing(entities, 150, 18),
    ...positionRing(documents, 305, 24),
  ];
}

function nodeColor(type: string, selected: boolean) {
  if (selected) return 'bg-black';
  if (type === 'DOCUMENT') return 'bg-sky-400';
  if (type === 'TECHNOLOGY') return 'bg-emerald-400';
  if (type === 'PROJECT') return 'bg-violet-400';
  if (type === 'ORGANIZATION') return 'bg-rose-400';
  return 'bg-amber-400';
}

function GraphContent() {
  const searchParams = useSearchParams();
  const initialEntity = searchParams.get('entity') || '';
  const containerRef = useRef<HTMLDivElement>(null);
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [links, setLinks] = useState<GraphLink[]>([]);
  const [searchQuery, setSearchQuery] = useState(initialEntity);
  const [selectedType, setSelectedType] = useState('ALL');
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [depth, setDepth] = useState(2);
  const [zoomLevel, setZoomLevel] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [panStart, setPanStart] = useState({ x: 0, y: 0 });
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [stats, setStats] = useState({ nodes: 0, links: 0, entities: 0, documents: 0 });
  const [refreshKey, setRefreshKey] = useState(0);
  const [copiedQuery, setCopiedQuery] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setIsLoading(true);
      setLoadError(null);
      try {
        const query = new URLSearchParams({ depth: String(depth), limit: '200' });
        if (searchQuery.trim()) query.set('query', searchQuery.trim());
        const response = await gatewayFetch(`/graph?${query.toString()}`, { signal: controller.signal });
        const data: GraphResponse = await response.json();
        if (!response.ok) throw new Error(data.detail || data.message || 'Knowledge graph could not be loaded.');
        const positioned = layoutNodes(data.nodes || []);
        setNodes(positioned);
        setLinks(data.links || []);
        setStats(data.stats || { nodes: positioned.length, links: 0, entities: 0, documents: 0 });
        setTruncated(Boolean(data.truncated));
        setSelectedNode((current) => positioned.find((node) => node.id === current?.id) || positioned[0] || null);
      } catch (error) {
        if (controller.signal.aborted) return;
        setNodes([]);
        setLinks([]);
        setSelectedNode(null);
        setLoadError(error instanceof Error ? error.message : 'Knowledge graph could not be loaded.');
      } finally {
        if (!controller.signal.aborted) setIsLoading(false);
      }
    }, 300);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [depth, refreshKey, searchQuery]);

  const visibleNodes = useMemo(
    () => nodes.filter((node) => selectedType === 'ALL' || node.type === selectedType),
    [nodes, selectedType],
  );
  const visibleNodeIds = useMemo(() => new Set(visibleNodes.map((node) => node.id)), [visibleNodes]);
  const activeLinks = useMemo(
    () => links.filter((link) => visibleNodeIds.has(link.source) && visibleNodeIds.has(link.target)),
    [links, visibleNodeIds],
  );
  const selectedNodeLinks = selectedNode
    ? links.filter((link) => link.source === selectedNode.id || link.target === selectedNode.id)
    : [];
  const availableTypes = useMemo(
    () => Array.from(new Set(nodes.map((node) => node.type))).sort(),
    [nodes],
  );

  const canvasCoordinates = (event: React.PointerEvent) => {
    const rect = containerRef.current?.getBoundingClientRect();
    return {
      x: (event.clientX - (rect?.left || 0) - pan.x) / zoomLevel,
      y: (event.clientY - (rect?.top || 0) - pan.y) / zoomLevel,
    };
  };

  const handlePointerDownNode = (event: React.PointerEvent, node: GraphNode) => {
    event.stopPropagation();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    const point = canvasCoordinates(event);
    setDraggingNodeId(node.id);
    setSelectedNode(node);
    setDragOffset({ x: point.x - node.x, y: point.y - node.y });
  };

  const handlePointerMove = (event: React.PointerEvent) => {
    if (isPanning) {
      setPan({ x: event.clientX - panStart.x, y: event.clientY - panStart.y });
      return;
    }
    if (!draggingNodeId) return;
    const point = canvasCoordinates(event);
    setNodes((current) => current.map((node) => node.id === draggingNodeId
      ? { ...node, x: point.x - dragOffset.x, y: point.y - dragOffset.y }
      : node));
  };

  const resetView = () => {
    setZoomLevel(1);
    setPan({ x: 0, y: 0 });
  };

  const cypherExample = `MATCH (entity:Entity {tenant_id: $tenantId, normalized_name: $normalizedName})\nMATCH (document:Document)-[:MENTIONS]->(entity)\nWHERE document.deleted_at IS NULL\nRETURN document, entity LIMIT 25;`;
  const copyCypher = async () => {
    await navigator.clipboard.writeText(cypherExample);
    setCopiedQuery(true);
    window.setTimeout(() => setCopiedQuery(false), 2000);
  };

  return (
    <div className="flex h-[calc(100vh-5rem)] max-w-7xl flex-col space-y-3.5 mx-auto">
      <div className="flex flex-col justify-between gap-3 px-1 sm:flex-row sm:items-center">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-white">Knowledge Graph</h1>
          <p className="mt-0.5 text-xs text-zinc-400">
            Live tenant-scoped entities and the documents that connect them.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-52">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
            <input
              type="search"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Find an entity or document..."
              className="w-full rounded-lg border border-zinc-800 bg-zinc-900 py-1.5 pl-8 pr-2.5 text-xs text-zinc-200 placeholder-zinc-500 focus:border-zinc-600 focus:outline-none"
            />
          </div>
          <select
            value={depth}
            onChange={(event) => setDepth(Number(event.target.value))}
            className="rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-300"
            aria-label="Traversal depth"
          >
            {[1, 2, 3, 4].map((value) => <option key={value} value={value}>{value}-hop</option>)}
          </select>
          <select
            value={selectedType}
            onChange={(event) => setSelectedType(event.target.value)}
            className="rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-300"
            aria-label="Entity type"
          >
            <option value="ALL">All types</option>
            {availableTypes.map((type) => <option key={type} value={type}>{type.toLowerCase()}</option>)}
          </select>
          <button
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            className="rounded-lg border border-zinc-800 bg-zinc-900 p-1.5 text-zinc-400 hover:text-white"
            title="Refresh graph"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
          </button>
          <div className="flex items-center rounded-lg border border-zinc-800 bg-zinc-900 p-0.5">
            <button type="button" onClick={() => setZoomLevel((value) => Math.max(0.45, value - 0.1))} className="p-1 text-zinc-400 hover:text-white" title="Zoom out"><ZoomOut className="h-3.5 w-3.5" /></button>
            <span className="px-1 text-[10px] font-mono text-zinc-400">{Math.round(zoomLevel * 100)}%</span>
            <button type="button" onClick={() => setZoomLevel((value) => Math.min(1.8, value + 0.1))} className="p-1 text-zinc-400 hover:text-white" title="Zoom in"><ZoomIn className="h-3.5 w-3.5" /></button>
            <button type="button" onClick={resetView} className="ml-0.5 border-l border-zinc-800 p-1 text-zinc-400 hover:text-white" title="Reset view"><Maximize2 className="h-3.5 w-3.5" /></button>
          </div>
        </div>
      </div>

      {loadError && (
        <div className="flex items-center gap-2 rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          <AlertCircle className="h-4 w-4" /> {loadError}
        </div>
      )}
      {truncated && (
        <div className="rounded-lg border border-amber-900/50 bg-amber-950/20 px-3 py-2 text-xs text-amber-300">
          This view reached the 200-node safety limit. Search for an entity to focus the graph.
        </div>
      )}

      <div className="relative flex min-h-0 flex-1 gap-4">
        <div
          ref={containerRef}
          onPointerDown={(event) => {
            setIsPanning(true);
            setPanStart({ x: event.clientX - pan.x, y: event.clientY - pan.y });
          }}
          onPointerMove={handlePointerMove}
          onPointerUp={() => { setIsPanning(false); setDraggingNodeId(null); }}
          onPointerCancel={() => { setIsPanning(false); setDraggingNodeId(null); }}
          className="relative flex-1 cursor-grab select-none overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-950 active:cursor-grabbing"
        >
          <div className="pointer-events-none absolute inset-0 opacity-[0.03]" style={{ backgroundImage: 'radial-gradient(#fff 1px, transparent 1px)', backgroundSize: '24px 24px' }} />
          {isLoading && nodes.length === 0 && <div className="absolute inset-0 grid place-items-center text-xs text-zinc-500">Loading workspace graph...</div>}
          {!isLoading && !loadError && nodes.length === 0 && (
            <div className="absolute inset-0 grid place-items-center px-6 text-center text-xs text-zinc-500">
              {searchQuery ? 'No graph nodes match this search.' : 'Upload documents to extract entities and build the graph.'}
            </div>
          )}
          <div
            className="relative h-[900px] w-[1400px] will-change-transform"
            style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoomLevel})`, transformOrigin: '0 0' }}
          >
            <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
              {activeLinks.map((link) => {
                const source = nodes.find((node) => node.id === link.source);
                const target = nodes.find((node) => node.id === link.target);
                if (!source || !target) return null;
                const highlighted = selectedNode && (selectedNode.id === source.id || selectedNode.id === target.id);
                return (
                  <line
                    key={link.id}
                    x1={source.x + 55}
                    y1={source.y + 16}
                    x2={target.x + 55}
                    y2={target.y + 16}
                    stroke={highlighted ? '#a1a1aa' : '#27272a'}
                    strokeWidth={highlighted ? Math.min(1 + link.weight / 3, 3) : 1}
                    strokeOpacity={highlighted ? 0.9 : 0.55}
                  />
                );
              })}
            </svg>
            {visibleNodes.map((node) => {
              const selected = selectedNode?.id === node.id;
              return (
                <button
                  type="button"
                  key={node.id}
                  onPointerDown={(event) => handlePointerDownNode(event, node)}
                  style={{ transform: `translate3d(${node.x}px, ${node.y}px, 0)`, touchAction: 'none' }}
                  className={`absolute left-0 top-0 z-20 flex max-w-52 cursor-move items-center gap-2 rounded-xl border px-3 py-1.5 text-xs transition-colors ${selected ? 'border-white bg-white text-black shadow-[0_0_20px_rgba(255,255,255,0.2)]' : 'border-zinc-800 bg-zinc-900/95 text-zinc-200 hover:border-zinc-600'}`}
                >
                  <span className={`h-2 w-2 shrink-0 rounded-full ${nodeColor(node.type, selected)}`} />
                  <span className="truncate font-medium">{node.name}</span>
                  <span className={`font-mono text-[10px] ${selected ? 'text-zinc-600' : 'text-zinc-500'}`}>{node.mention_count}</span>
                </button>
              );
            })}
          </div>
          <div className="pointer-events-none absolute bottom-3 left-3 rounded-lg border border-zinc-800 bg-zinc-900/90 px-3 py-1.5 text-[11px] text-zinc-400">
            {stats.entities} entities · {stats.documents} documents · {stats.links} relationships
          </div>
        </div>

        <aside className="w-80 shrink-0 space-y-4 overflow-y-auto rounded-2xl border border-zinc-800/80 bg-zinc-950 p-5">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-3 text-xs">
            <span className="flex items-center gap-1.5 font-medium text-white"><Info className="h-3.5 w-3.5 text-zinc-400" />Node Inspector</span>
            <span className="font-mono text-[10px] text-zinc-500">live Neo4j</span>
          </div>
          {selectedNode ? (
            <div className="space-y-4">
              <div>
                <span className="rounded border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-[10px] font-mono text-zinc-300">{selectedNode.type}</span>
                <h2 className="mt-2 text-base font-semibold text-white">{selectedNode.name}</h2>
                <p className="mt-1 text-xs text-zinc-500">
                  {selectedNode.mention_count} mentions · {selectedNode.document_count} linked {selectedNode.document_count === 1 ? 'document' : 'documents'}
                </p>
                {selectedNode.type === 'DOCUMENT' && (
                  <Link href={`/documents/${selectedNode.record_id}`} className="mt-2 inline-flex items-center gap-1 text-xs text-sky-300 hover:text-sky-200">
                    <FileText className="h-3 w-3" /> Inspect document
                  </Link>
                )}
              </div>
              <div className="space-y-2">
                <div className="flex justify-between text-xs text-zinc-300"><span>Connected nodes</span><span className="font-mono text-zinc-500">{selectedNodeLinks.length}</span></div>
                {selectedNodeLinks.length === 0 && <p className="text-xs text-zinc-500">No visible relationships.</p>}
                {selectedNodeLinks.map((link) => {
                  const otherId = link.source === selectedNode.id ? link.target : link.source;
                  const otherNode = nodes.find((node) => node.id === otherId);
                  if (!otherNode) return null;
                  return (
                    <button
                      type="button"
                      key={link.id}
                      onClick={() => setSelectedNode(otherNode)}
                      className="flex w-full items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900/70 p-2.5 text-left text-xs hover:bg-zinc-800"
                    >
                      <span className="min-w-0 truncate text-zinc-200">{otherNode.name}</span>
                      <ChevronRight className="h-3 w-3 shrink-0 text-zinc-500" />
                    </button>
                  );
                })}
              </div>
              <div className="space-y-1.5 border-t border-zinc-800 pt-3">
                <div className="flex items-center justify-between text-xs text-zinc-300">
                  <span>Parameterized Cypher</span>
                  <button type="button" onClick={copyCypher} className="flex items-center gap-1 text-[11px] text-zinc-500 hover:text-white">
                    {copiedQuery ? <><Check className="h-3 w-3 text-emerald-400" />Copied</> : <><Copy className="h-3 w-3" />Copy</>}
                  </button>
                </div>
                <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-[10px] leading-relaxed text-zinc-400">{cypherExample}</pre>
              </div>
            </div>
          ) : (
            <p className="text-xs text-zinc-500">Select a graph node to inspect its live relationships.</p>
          )}
        </aside>
      </div>
    </div>
  );
}

export default function GraphExplorerPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading knowledge graph...</div>}>
      <GraphContent />
    </Suspense>
  );
}
