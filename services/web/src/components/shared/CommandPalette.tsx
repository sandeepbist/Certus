'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  Activity,
  Brain,
  CheckSquare,
  FileText,
  Layers,
  MessageSquare,
  Search,
  Settings,
  Upload,
  X,
} from 'lucide-react';

import { searchWorkspace, WorkspaceSearchResult } from '@/lib/workspace-search';

export const OPEN_COMMAND_PALETTE_EVENT = 'certus:open-command-palette';

interface PaletteItem {
  id: string;
  title: string;
  detail: string;
  icon: React.ComponentType<{ className?: string }>;
  action: () => void;
}
const resultIcons = { documents: FileText, tasks: CheckSquare, memories: Brain, chat: Activity };
const resultLabels = { documents: 'Document', tasks: 'Task', memories: 'Memory', chat: 'Chat' };

export function CommandPalette() {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<WorkspaceSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const router = useRouter();

  const close = useCallback(() => {
    setIsOpen(false);
    setQuery('');
    setSearchResults([]);
    setSearchError(null);
  }, []);
  const navigate = useCallback((href: string) => {
    router.push(href);
    close();
  }, [close, router]);

  useEffect(() => {
    const openPalette = () => setIsOpen(true);
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setIsOpen((open) => !open);
      }
      if (event.key === 'Escape') close();
    };
    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, openPalette);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, openPalette);
    };
  }, [close]);

  useEffect(() => {
    if (!isOpen || query.trim().length < 2) {
      setSearchResults([]);
      setSearchError(null);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      setSearching(true);
      setSearchError(null);
      searchWorkspace(query.trim(), { limitPerType: 3, signal: controller.signal })
        .then((response) => setSearchResults(response.results.slice(0, 8)))
        .catch((error) => {
          if (error instanceof DOMException && error.name === 'AbortError') return;
          setSearchResults([]);
          setSearchError(error instanceof Error ? error.message : 'Workspace search failed.');
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [isOpen, query]);

  const commands = useMemo<PaletteItem[]>(() => [
    { id: 'new_chat', title: 'New conversation', detail: 'Navigation', icon: MessageSquare, action: () => navigate('/chat') },
    { id: 'upload_doc', title: 'Upload documents', detail: 'Action', icon: Upload, action: () => navigate('/documents/upload') },
    { id: 'tasks', title: 'Manage tasks', detail: 'Navigation', icon: CheckSquare, action: () => navigate('/tasks') },
    { id: 'traces', title: 'View agent traces', detail: 'Telemetry', icon: Activity, action: () => navigate('/traces') },
    { id: 'graph', title: 'Explore knowledge graph', detail: 'Knowledge', icon: Layers, action: () => navigate('/graph') },
    { id: 'settings', title: 'Workspace settings', detail: 'System', icon: Settings, action: () => navigate('/settings') },
  ], [navigate]);

  const filteredCommands = commands.filter((command) => (
    command.title.toLowerCase().includes(query.toLowerCase())
    || command.detail.toLowerCase().includes(query.toLowerCase())
  ));
  const paletteItems: PaletteItem[] = [
    ...(query.trim().length >= 2 ? [{
      id: 'full_search',
      title: `Search workspace for “${query.trim()}”`,
      detail: 'All results',
      icon: Search,
      action: () => navigate(`/search?q=${encodeURIComponent(query.trim())}`),
    }] : []),
    ...searchResults.map((result) => ({
      id: `${result.type}-${result.id}`,
      title: result.title,
      detail: resultLabels[result.type],
      icon: resultIcons[result.type],
      action: () => navigate(result.href),
    })),
    ...filteredCommands,
  ];

  useEffect(() => {
    setActiveIndex(0);
  }, [query, searchResults.length, isOpen]);

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Command palette and workspace search"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/80 px-4 pt-24 backdrop-blur-md"
    >
      <div className="flex w-full max-w-xl flex-col overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950 shadow-2xl">
        <div className="flex items-center border-b border-zinc-800 px-4 py-3">
          <Search className="mr-2.5 h-4 w-4 text-zinc-500" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') {
                event.preventDefault();
                setActiveIndex((index) => Math.min(index + 1, Math.max(0, paletteItems.length - 1)));
              } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setActiveIndex((index) => Math.max(index - 1, 0));
              } else if (event.key === 'Enter' && paletteItems[activeIndex]) {
                event.preventDefault();
                paletteItems[activeIndex].action();
              }
            }}
            placeholder="Search workspace or run a command…"
            aria-label="Search workspace or commands"
            autoFocus
            className="flex-1 bg-transparent text-xs text-white placeholder-zinc-500 outline-none"
          />
          <button type="button" onClick={close} aria-label="Close command palette" className="p-1 text-zinc-500 hover:text-white"><X className="h-4 w-4" /></button>
        </div>

        <div className="max-h-96 overflow-y-auto p-1.5">
          {searching && <div className="px-3 py-2 text-[10px] text-zinc-600">Searching indexed workspace data…</div>}
          {searchError && <div className="m-1 rounded border border-red-950 bg-red-950/30 px-3 py-2 text-[10px] text-red-300">{searchError}</div>}
          {paletteItems.length === 0 ? (
            <div className="py-8 text-center text-xs text-zinc-500">No matching commands or workspace records.</div>
          ) : paletteItems.map((item, index) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                onMouseEnter={() => setActiveIndex(index)}
                onClick={item.action}
                className={`flex w-full items-center rounded-lg px-3 py-2 text-left text-xs transition-colors ${
                  index === activeIndex ? 'bg-zinc-900 text-white' : 'text-zinc-300 hover:bg-zinc-900'
                }`}
              >
                <Icon className="mr-2.5 h-3.5 w-3.5 shrink-0 text-zinc-500" />
                <span className="min-w-0 flex-1 truncate font-medium">{item.title}</span>
                <span className="ml-3 text-[10px] text-zinc-600">{item.detail}</span>
              </button>
            );
          })}
        </div>

        <div className="flex items-center justify-between border-t border-zinc-800 bg-zinc-900 px-3.5 py-1.5 text-[10px] text-zinc-500">
          <span>↑↓ choose · Enter open · Esc close</span>
          <span className="font-mono">Certus Search</span>
        </div>
      </div>
    </div>
  );
}
