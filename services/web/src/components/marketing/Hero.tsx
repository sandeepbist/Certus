import Link from 'next/link';
import { ArrowRight, CheckCircle, Database, Shield, Zap } from 'lucide-react';

export function Hero() {
  return (
    <section className="relative pt-28 pb-16 px-6 md:px-12 max-w-5xl mx-auto flex flex-col items-center text-center">
      {/* Pill badge */}
      <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-zinc-900 border border-zinc-800 text-zinc-300 text-xs font-mono mb-6">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
        Certus v1.0 • Verifiable Intelligence & Continuous Intent Assurance
      </div>

      {/* Main Headline */}
      <h1 className="text-3xl sm:text-5xl lg:text-6xl font-bold tracking-tight text-white max-w-3xl leading-tight mb-4">
        Grounded Intelligence. <br />
        <span className="text-zinc-400">Verifiable Autonomous Action.</span>
      </h1>

      {/* Subheading */}
      <p className="text-sm sm:text-base text-zinc-400 max-w-xl font-normal leading-relaxed mb-8">
        Certus connects your enterprise knowledge, extracts multi-hop entity graphs, enforces deterministic intent contracts before agent tool execution, and verifies postcondition business states.
      </p>

      {/* CTA Buttons */}
      <div className="flex flex-col sm:flex-row items-center gap-3 mb-12 w-full sm:w-auto">
        <Link
          href="/dashboard"
          className="w-full sm:w-auto flex items-center justify-center gap-1.5 px-5 py-2.5 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors"
        >
          <span>Open Workspace</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </Link>
        <Link
          href="/chat"
          className="w-full sm:w-auto flex items-center justify-center gap-1.5 px-5 py-2.5 rounded-lg bg-zinc-900 hover:bg-zinc-850 text-zinc-200 border border-zinc-800 font-medium text-xs transition-colors"
        >
          <span>Try Interactive Query</span>
        </Link>
      </div>

      {/* Key Guarantees */}
      <div className="flex flex-wrap items-center justify-center gap-6 text-xs text-zinc-400 mb-12 font-medium">
        <div className="flex items-center gap-1.5">
          <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
          <span>Grounded Hybrid Retrieval (Zero Hallucinations)</span>
        </div>
        <div className="flex items-center gap-1.5">
          <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
          <span>Deterministic Pre-Action Gate</span>
        </div>
        <div className="flex items-center gap-1.5">
          <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
          <span>Durable Temporal Workflows</span>
        </div>
      </div>

      {/* Architecture Matrix */}
      <div className="w-full rounded-xl bg-zinc-950 border border-zinc-800/80 p-4 text-left">
        <div className="flex items-center justify-between pb-3 border-b border-zinc-850 mb-3 text-xs font-mono">
          <span className="text-zinc-400">Certus Core Operating Pillars</span>
          <span className="text-emerald-500 font-mono">ACTIVE</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800">
            <div className="flex items-center gap-2 text-zinc-200 mb-1.5">
              <Database className="w-3.5 h-3.5 text-zinc-400" />
              <span className="font-semibold text-xs">Grounded Knowledge Base</span>
            </div>
            <p className="text-zinc-400 text-[11px] leading-relaxed">
              pgvector HNSW cosine search fused with BM25 keyword matching and Neo4j multi-hop entity graphs.
            </p>
          </div>

          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800">
            <div className="flex items-center gap-2 text-zinc-200 mb-1.5">
              <Shield className="w-3.5 h-3.5 text-zinc-400" />
              <span className="font-semibold text-xs">Intent Action Contracts</span>
            </div>
            <p className="text-zinc-400 text-[11px] leading-relaxed">
              Compile decisions into versioned contracts; enforce deterministic policy checks before any tool dispatch.
            </p>
          </div>

          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800">
            <div className="flex items-center gap-2 text-zinc-200 mb-1.5">
              <Zap className="w-3.5 h-3.5 text-zinc-400" />
              <span className="font-semibold text-xs">Durable Execution & Replay</span>
            </div>
            <p className="text-zinc-400 text-[11px] leading-relaxed">
              Temporal state persistence, MCP tool integration, and time-travel trace replay when policies evolve.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
