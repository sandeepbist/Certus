'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  Activity,
  CheckSquare,
  FileText,
  MessageSquare,
  Plus,
  Send,
  Upload,
  Zap,
} from 'lucide-react';

import { useSession } from '@/lib/auth-client';
import { gatewayFetch } from '@/lib/gateway-client';
import { useStreamingChat } from '@/hooks/useStreamingChat';

type Overview = {
  metrics: {
    documents: number;
    documents_this_week: number;
    queries: number;
    avg_claim_evidence_integrity: number | null;
    active_tasks: number;
    due_today: number;
    tokens_today: number;
    token_budget: number;
    cost_today_usd: number;
  };
  recent_activity: Array<{
    activity_type: string;
    activity_id: string;
    title: string;
    detail: string;
    status: string;
    created_at: string;
    href: string;
  }>;
};

type Health = {
  status: 'healthy' | 'degraded';
  checked_at: string;
  services: Array<{ name: string; status: 'healthy' | 'unavailable'; latency_ms: number }>;
};

function DashboardContent() {
  const { data: session } = useSession();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [dashboardError, setDashboardError] = useState<string | null>(null);
  const [inputQuery, setInputQuery] = useState('');
  const {
    messages,
    agentEvents,
    isStreaming,
    sendMessage,
    cancel,
    completedRunId,
  } = useStreamingChat({
    welcomeMessage: 'Ask a question across your workspace documents, graph, tasks, and saved memories.',
  });

  useEffect(() => {
    let cancelled = false;
    async function loadDashboard() {
      const [overviewResponse, healthResponse] = await Promise.all([
        gatewayFetch('/dashboard/overview'),
        gatewayFetch('/dashboard/health'),
      ]);
      const overviewPayload = await overviewResponse.json().catch(() => null);
      const healthPayload = await healthResponse.json().catch(() => null);
      if (!overviewResponse.ok) {
        throw new Error(overviewPayload?.message || overviewPayload?.detail || 'Workspace metrics could not be loaded.');
      }
      if (!cancelled) {
        setOverview(overviewPayload as Overview);
        if (healthResponse.ok) setHealth(healthPayload as Health);
      }
    }
    loadDashboard().catch((error) => {
      if (!cancelled) {
        setDashboardError(error instanceof Error ? error.message : 'Workspace metrics could not be loaded.');
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!completedRunId) return;
    const controller = new AbortController();
    void gatewayFetch('/dashboard/overview', { signal: controller.signal }).then(async (response) => {
      if (response.ok) setOverview(await response.json());
    }).catch(() => undefined);
    return () => controller.abort();
  }, [completedRunId]);

  const handleSendQuery = (event: React.FormEvent) => {
    event.preventDefault();
    if (!inputQuery.trim() || isStreaming) return;
    const query = inputQuery.trim();
    setInputQuery('');
    void sendMessage(query);
  };

  const metrics = overview?.metrics;

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-white tracking-tight">Overview</h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            {session?.user.name ? 'Welcome back, ' + session.user.name + '.' : 'Your current workspace at a glance.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => document.getElementById('dashboard-chat-input')?.focus()} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black hover:bg-zinc-200 text-xs font-medium">
            <Plus className="w-3.5 h-3.5" /> New chat
          </button>
          <Link href="/documents/upload" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-200 border border-zinc-800 text-xs font-medium">
            <Upload className="w-3.5 h-3.5" /> Upload
          </Link>
          <Link href="/tasks" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-200 border border-zinc-800 text-xs font-medium">
            <CheckSquare className="w-3.5 h-3.5" /> Tasks
          </Link>
        </div>
      </div>

      {dashboardError && (
        <div role="alert" className="p-3 rounded-lg border border-red-900/60 bg-red-950/30 text-xs text-red-300">{dashboardError}</div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Metric label="Documents" value={metrics?.documents} icon={FileText} detail={metrics ? metrics.documents_this_week + ' added in 7 days' : 'Loading…'} />
        <Metric label="Agent queries" value={metrics?.queries} icon={MessageSquare} detail={metrics?.avg_claim_evidence_integrity == null ? 'No v1 grounded runs yet' : (metrics.avg_claim_evidence_integrity * 100).toFixed(0) + '% claim-link integrity'} />
        <Metric label="Active tasks" value={metrics?.active_tasks} icon={CheckSquare} detail={metrics ? metrics.due_today + ' due today' : 'Loading…'} />
        <Metric label="Tokens today" value={metrics?.tokens_today} icon={Zap} detail={metrics ? metrics.token_budget.toLocaleString() + ' daily budget' : 'Loading…'} />
        <Metric label="Est. spend today" value={metrics ? '$' + metrics.cost_today_usd.toFixed(4) : undefined} icon={Activity} detail="Measured run total" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div className="lg:col-span-2 bg-zinc-950 border border-zinc-800/80 rounded-xl p-4 flex flex-col h-[480px]">
          <div className="flex items-center justify-between pb-3 border-b border-zinc-800 mb-3">
            <span className="font-medium text-white text-xs">Workspace assistant</span>
            <Link href="/chat" className="text-[10px] text-zinc-500 hover:text-white">Open full chat</Link>
          </div>
          <div className="flex-1 overflow-y-auto space-y-3 pr-2">
            {messages.map((message) => (
              <div key={message.id} className={'flex gap-2.5 text-xs leading-relaxed ' + (message.role === 'user' ? 'justify-end' : 'justify-start')}>
                <div className={'p-3 rounded-lg max-w-[85%] whitespace-pre-wrap ' + (message.role === 'user' ? 'bg-zinc-100 text-black font-medium' : 'bg-zinc-900 border border-zinc-800 text-zinc-200')}>
                  {message.content || (isStreaming ? 'Working…' : '')}
                </div>
              </div>
            ))}
            {agentEvents.length > 0 && (
              <div className="p-2.5 rounded-lg bg-zinc-900 border border-zinc-800 text-[11px] font-mono text-zinc-400 space-y-1">
                {agentEvents.map((item, index) => <div key={index}>[{item.agent}] {item.action}</div>)}
              </div>
            )}
          </div>
          <form onSubmit={handleSendQuery} className="mt-3 pt-3 border-t border-zinc-800 flex gap-2">
            <input id="dashboard-chat-input" value={inputQuery} onChange={(event) => setInputQuery(event.target.value)} placeholder="Ask anything across your workspace…" className="flex-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500" />
            <button type={isStreaming ? 'button' : 'submit'} onClick={isStreaming ? cancel : undefined} disabled={!isStreaming && !inputQuery.trim()} className="px-3.5 py-2 rounded-lg bg-white text-black disabled:opacity-40 text-xs font-medium flex items-center gap-1.5">{isStreaming ? 'Stop' : 'Send'} {!isStreaming && <Send className="w-3 h-3" />}</button>
          </form>
        </div>

        <div className="space-y-4">
          <div className="bg-zinc-950 border border-zinc-800/80 p-4 rounded-xl">
            <div className="flex items-center justify-between mb-3">
              <span className="font-medium text-white text-xs">Infrastructure</span>
              <span className={'text-[10px] font-mono ' + (health?.status === 'healthy' ? 'text-emerald-400' : health ? 'text-amber-400' : 'text-zinc-500')}>
                {health?.status || 'checking'}
              </span>
            </div>
            <div className="space-y-2">
              {health?.services.map((service) => (
                <div key={service.name} className="flex items-center justify-between p-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs">
                  <span className="text-zinc-300">{service.name}</span>
                  <span className="flex items-center gap-2 text-[10px] text-zinc-500 font-mono">
                    {service.latency_ms}ms
                    <span className={'w-1.5 h-1.5 rounded-full ' + (service.status === 'healthy' ? 'bg-emerald-500' : 'bg-red-500')} />
                  </span>
                </div>
              )) || <p className="text-xs text-zinc-500">Checking dependencies…</p>}
            </div>
          </div>

          <div className="bg-zinc-950 border border-zinc-800/80 p-4 rounded-xl">
            <span className="font-medium text-white text-xs block mb-3">Recent activity</span>
            <div className="space-y-2.5">
              {overview?.recent_activity.length === 0 && <p className="text-xs text-zinc-500">No workspace activity yet.</p>}
              {overview?.recent_activity.slice(0, 6).map((item) => (
                <Link key={item.activity_type + item.activity_id} href={item.href} className="block p-2 rounded-lg hover:bg-zinc-900 transition-colors">
                  <p className="text-xs text-zinc-200 truncate">{item.title}</p>
                  <p className="text-[10px] text-zinc-500 truncate">{item.activity_type} · {item.detail} · {new Date(item.created_at).toLocaleString()}</p>
                </Link>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, detail, icon: Icon }: { label: string; value: string | number | undefined; detail: string; icon: React.ComponentType<{ className?: string }> }) {
  return (
    <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
      <div className="flex items-center justify-between text-zinc-400 text-xs mb-1.5"><span>{label}</span><Icon className="w-3.5 h-3.5" /></div>
      <p className="text-xl font-semibold text-white font-mono">{typeof value === 'number' ? value.toLocaleString() : value ?? '—'}</p>
      <p className="text-[10px] text-zinc-500 mt-1">{detail}</p>
    </div>
  );
}

export default function DashboardPage() {
  return <DashboardContent />;
}
