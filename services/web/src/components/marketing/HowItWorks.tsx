import { UploadCloud, Bot, ShieldCheck } from 'lucide-react';

export function HowItWorks() {
  const steps = [
    {
      num: '01',
      icon: UploadCloud,
      title: 'Ingest & Entity Extraction',
      desc: 'Drop in PDFs, Markdown, DOCX, or code. Certus chunks content with contextual headers, embeds into pgvector, and maps entity relations in Neo4j.',
    },
    {
      num: '02',
      icon: Bot,
      title: 'Multi-Agent Intent Synthesis',
      desc: 'LangGraph agents collaborate across dense vectors, keyword indices, and multi-hop graph paths to synthesize grounded answers with citations.',
    },
    {
      num: '03',
      icon: ShieldCheck,
      title: 'Governed Action & Verification',
      desc: 'Agent decisions compile into executable intent contracts. Actions execute durably via Temporal + MCP and produce verifiable state receipts.',
    },
  ];

  return (
    <section id="architecture" className="py-20 px-6 md:px-12 max-w-5xl mx-auto border-t border-zinc-850">
      <div className="text-center max-w-2xl mx-auto mb-12">
        <h2 className="text-2xl sm:text-3xl font-bold tracking-tight text-white mb-2">
          How Certus Operates
        </h2>
        <p className="text-zinc-400 text-xs sm:text-sm leading-relaxed">
          From unstructured data to verifiable, autonomous action in three steps.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 relative">
        {steps.map((step) => {
          const Icon = step.icon;
          return (
            <div
              key={step.num}
              className="p-6 rounded-xl bg-zinc-950 border border-zinc-800/80 flex flex-col justify-between"
            >
              <div className="flex items-center justify-between mb-4">
                <span className="text-2xl font-bold font-mono text-zinc-700">
                  {step.num}
                </span>
                <div className="w-8 h-8 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300">
                  <Icon className="w-4 h-4" />
                </div>
              </div>
              <div>
                <h3 className="text-sm font-semibold text-white mb-1.5">{step.title}</h3>
                <p className="text-zinc-400 text-xs leading-relaxed">{step.desc}</p>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
