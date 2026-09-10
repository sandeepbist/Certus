import { Github, Twitter } from 'lucide-react';

export function Footer() {
  return (
    <footer className="border-t border-zinc-800/80 bg-black px-6 md:px-12 py-12 text-xs text-zinc-500">
      <div className="max-w-5xl mx-auto grid grid-cols-2 md:grid-cols-4 gap-8 mb-10">
        <div className="col-span-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="font-semibold text-white tracking-tight text-sm">Certus</span>
            <span className="text-[10px] px-1.5 py-0.2 rounded bg-zinc-900 text-zinc-400 font-mono border border-zinc-800">
              v1.0
            </span>
          </div>
          <p className="text-zinc-400 max-w-sm text-xs leading-relaxed mb-4">
            Intent-assured autonomous operating platform uniting hybrid RAG, Neo4j GraphRAG, and verifiable execution contracts.
          </p>
          <div className="flex items-center gap-3 text-zinc-400">
            <a href="https://github.com" target="_blank" rel="noreferrer" className="hover:text-white transition-colors">
              <Github className="w-4 h-4" />
            </a>
            <a href="https://twitter.com" target="_blank" rel="noreferrer" className="hover:text-white transition-colors">
              <Twitter className="w-4 h-4" />
            </a>
          </div>
        </div>

        <div>
          <p className="font-medium text-zinc-300 uppercase tracking-wider text-[10px] mb-2.5 font-mono">Capabilities</p>
          <ul className="space-y-2">
            <li><a href="#features" className="hover:text-white transition-colors">Hybrid RAG</a></li>
            <li><a href="#features" className="hover:text-white transition-colors">Multi-Agent Graph</a></li>
            <li><a href="#features" className="hover:text-white transition-colors">Intent Contracts</a></li>
            <li><a href="#pricing" className="hover:text-white transition-colors">Pricing</a></li>
          </ul>
        </div>

        <div>
          <p className="font-medium text-zinc-300 uppercase tracking-wider text-[10px] mb-2.5 font-mono">Infrastructure</p>
          <ul className="space-y-2">
            <li><span className="text-zinc-400">pgvector HNSW</span></li>
            <li><span className="text-zinc-400">Neo4j Graph Engine</span></li>
            <li><span className="text-zinc-400">LangGraph StateGraph</span></li>
            <li><span className="text-zinc-400">Temporal Workflows</span></li>
          </ul>
        </div>
      </div>

      <div className="max-w-5xl mx-auto pt-6 border-t border-zinc-900 flex flex-col sm:flex-row items-center justify-between gap-3 text-[11px]">
        <p>© {new Date().getFullYear()} Certus. MIT License.</p>
        <p className="text-zinc-600 font-mono">Continuous Intent Assurance Substrate</p>
      </div>
    </footer>
  );
}
