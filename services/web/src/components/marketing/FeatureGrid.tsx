import { Brain, Bot, Network, Zap, Shield, Sparkles } from 'lucide-react';

export function FeatureGrid() {
  const features = [
    {
      icon: Brain,
      title: 'Hybrid RAG',
      desc: 'Combines HNSW dense vector similarity, tsvector full-text keyword indexing, and reciprocal rank fusion (RRF) with cross-encoder re-ranking.',
      tag: 'Dense + Sparse',
    },
    {
      icon: Bot,
      title: 'Multi-Agent Orchestration',
      desc: 'LangGraph-powered cyclic agent loop: Planner decomposes tasks, Researcher retrieves context, Executor synthesizes answers, and Critic validates grounding.',
      tag: 'LangGraph StateGraph',
    },
    {
      icon: Network,
      title: 'GraphRAG Knowledge Graph',
      desc: 'Neo4j graph database captures extracted entities (people, technologies, concepts) and their relationships for multi-hop reasoning.',
      tag: 'Neo4j + Cypher',
    },
    {
      icon: Shield,
      title: 'Continuous Intent Assurance',
      desc: 'Enforces deterministic policy gates before agent actions and generates immutable outcome receipts to verify business state.',
      tag: 'Action Contracts',
    },
    {
      icon: Zap,
      title: 'Durable Workflows',
      desc: 'Temporal engine integration ensures scheduled automations and event-driven pipelines survive server restarts with automatic compensations.',
      tag: 'Temporal.io',
    },
    {
      icon: Sparkles,
      title: 'Persistent Memory Store',
      desc: 'Short-term buffer for conversation session history and long-term semantic fact store with background consolidation and entity reflection.',
      tag: 'Memory Store',
    },
  ];

  return (
    <section id="features" className="py-20 px-6 md:px-12 max-w-5xl mx-auto">
      <div className="text-center max-w-2xl mx-auto mb-12">
        <h2 className="text-2xl sm:text-3xl font-bold tracking-tight text-white mb-2">
          Engineered for Intelligence & Reliability
        </h2>
        <p className="text-zinc-400 text-xs sm:text-sm leading-relaxed">
          Every layer of Certus is designed to eliminate hallucination, guarantee auditability, and ensure intent compliance.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {features.map((item) => {
          const Icon = item.icon;
          return (
            <div
              key={item.title}
              className="p-5 rounded-xl bg-zinc-950 border border-zinc-800/80 flex flex-col justify-between"
            >
              <div>
                <div className="flex items-center justify-between mb-3">
                  <div className="p-2 rounded-lg bg-zinc-900 border border-zinc-800">
                    <Icon className="w-4 h-4 text-zinc-300" />
                  </div>
                  <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-zinc-900 text-zinc-400 border border-zinc-800">
                    {item.tag}
                  </span>
                </div>
                <h3 className="text-sm font-semibold text-white mb-1.5">{item.title}</h3>
                <p className="text-zinc-400 text-xs leading-relaxed">{item.desc}</p>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
