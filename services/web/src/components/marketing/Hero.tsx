import Link from 'next/link';
import { ArrowRight, CheckCircle, Database, Shield, Zap } from 'lucide-react';

export function Hero() {
  return (
    <section className="relative pt-28 pb-16 px-6 md:px-12 max-w-5xl mx-auto flex flex-col items-center text-center">
      {/* Pill badge */}
      <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-zinc-900 border border-zinc-800 text-zinc-300 text-xs font-mono mb-6">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
        Certus • Workspace knowledge and tools
      </div>

      {/* Main Headline */}
      <h1 className="text-3xl sm:text-5xl lg:text-6xl font-bold tracking-tight text-white max-w-3xl leading-tight mb-4">
        Ask your documents. <br />
        <span className="text-zinc-400">Inspect the evidence.</span>
      </h1>

      {/* Subheading */}
      <p className="text-sm sm:text-base text-zinc-400 max-w-xl font-normal leading-relaxed mb-8">
        Ask questions about your workspace documents, narrow retrieval by scope, and use supported tools. Review answer evidence as you work.
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
          <span>Scoped document retrieval</span>
        </div>
        <div className="flex items-center gap-1.5">
          <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
          <span>Inspectable answer evidence</span>
        </div>
        <div className="flex items-center gap-1.5">
          <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
          <span>Durable workflow retries</span>
        </div>
      </div>

      {/* Architecture Matrix */}
      <div className="w-full rounded-xl bg-zinc-950 border border-zinc-800/80 p-4 text-left">
        <div className="flex items-center justify-between pb-3 border-b border-zinc-850 mb-3 text-xs font-mono">
          <span className="text-zinc-400">Inside Certus</span>
          <span className="text-emerald-500 font-mono">CAPABILITIES</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800">
            <div className="flex items-center gap-2 text-zinc-200 mb-1.5">
              <Database className="w-3.5 h-3.5 text-zinc-400" />
              <span className="font-semibold text-xs">Hybrid retrieval</span>
            </div>
            <p className="text-zinc-400 text-[11px] leading-relaxed">
              Combines pgvector similarity with PostgreSQL full-text search using reciprocal rank fusion.
            </p>
          </div>

          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800">
            <div className="flex items-center gap-2 text-zinc-200 mb-1.5">
              <Shield className="w-3.5 h-3.5 text-zinc-400" />
              <span className="font-semibold text-xs">Evidence checks</span>
            </div>
            <p className="text-zinc-400 text-[11px] leading-relaxed">
              Checks claim structure and evidence references so you can review the sources behind an answer.
            </p>
          </div>

          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800">
            <div className="flex items-center gap-2 text-zinc-200 mb-1.5">
              <Zap className="w-3.5 h-3.5 text-zinc-400" />
              <span className="font-semibold text-xs">Workflows and replay</span>
            </div>
            <p className="text-zinc-400 text-[11px] leading-relaxed">
              Temporal workflows retry and record outcomes. Replay generates a new answer from frozen evidence.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
