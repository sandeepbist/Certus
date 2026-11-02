import { Brain, Bot, Network, Zap, Shield, Sparkles } from 'lucide-react';

export function FeatureGrid() {
  const features = [
    {
      icon: Brain,
      title: 'Hybrid retrieval',
      desc: 'Combines pgvector similarity and PostgreSQL full-text search with reciprocal rank fusion to retrieve document context.',
      tag: 'Vector + full-text',
    },
    {
      icon: Bot,
      title: 'Agent pipeline',
      desc: 'A fixed LangGraph pipeline retrieves context, invokes supported MCP tools, generates answers, and checks claim and evidence structure.',
      tag: 'LangGraph pipeline',
    },
    {
      icon: Network,
      title: 'Knowledge graph',
      desc: 'Uses heuristic entity extraction and document co-mentions to build a Neo4j graph. Traversal follows related nodes within set limits.',
      tag: 'Neo4j + Cypher',
    },
    {
      icon: Shield,
      title: 'Evidence checks',
      desc: 'Checks claim structure and evidence references for consistency. Review the sources to judge whether an answer is correct.',
      tag: 'Claim + evidence',
    },
    {
      icon: Zap,
      title: 'Durable workflows',
      desc: 'Temporal workflows use retries and record execution outcomes.',
      tag: 'Temporal.io',
    },
    {
      icon: Sparkles,
      title: 'Persistent memory',
      desc: 'Stores facts within a selected scope. Run similarity analysis on demand to compare stored memories.',
      tag: 'Scoped facts',
    },
  ];

  return (
    <section id="features" className="py-20 px-6 md:px-12 max-w-5xl mx-auto">
      <div className="text-center max-w-2xl mx-auto mb-12">
        <h2 className="text-2xl sm:text-3xl font-bold tracking-tight text-white mb-2">
          Work with your documents and tools
        </h2>
        <p className="text-zinc-400 text-xs sm:text-sm leading-relaxed">
          Retrieve document context, inspect answer evidence, run durable workflows, and keep scoped facts for later use.
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
