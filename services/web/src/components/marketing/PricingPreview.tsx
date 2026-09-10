import Link from 'next/link';
import { Check } from 'lucide-react';

export function PricingPreview() {
  const tiers = [
    {
      name: 'Developer',
      price: '$0',
      period: 'forever',
      desc: 'Ideal for local evaluation, developers, and personal workspaces.',
      features: [
        'Up to 100 documents',
        '512MB pgvector index',
        '100K tokens / day',
        'PostgreSQL 17 hybrid RAG',
        'Single-tenant instance',
      ],
      cta: 'Start Free',
      href: '/sign-up',
      highlight: false,
    },
    {
      name: 'Team Pro',
      price: '$29',
      period: 'per month',
      desc: 'Full multi-agent capabilities, graph exploration, and durable workflows.',
      features: [
        'Up to 2,500 documents',
        '15GB high-speed storage',
        '1M tokens / day',
        'Neo4j Knowledge Graph',
        'LangGraph multi-agent teams',
        'Intent contract enforcement',
        'Temporal.io workflows',
        'Multi-tenant RBAC',
      ],
      cta: 'Get Started',
      href: '/sign-up',
      highlight: true,
    },
    {
      name: 'Enterprise',
      price: 'Custom',
      period: 'tailored SLA',
      desc: 'Dedicated infrastructure, custom LLM routing, and compliance receipts.',
      features: [
        'Unlimited documents & storage',
        'Dedicated K8s cluster',
        'Custom token budgets',
        'Independent postcondition verification',
        'SSO / SAML login',
        'GDPR export & audit trail',
        '24/7 dedicated support',
      ],
      cta: 'Contact Sales',
      href: '/sign-up',
      highlight: false,
    },
  ];

  return (
    <section id="pricing" className="py-20 px-6 md:px-12 max-w-5xl mx-auto border-t border-zinc-850">
      <div className="text-center max-w-2xl mx-auto mb-12">
        <h2 className="text-2xl sm:text-3xl font-bold tracking-tight text-white mb-2">
          Transparent Pricing
        </h2>
        <p className="text-zinc-400 text-xs sm:text-sm leading-relaxed">
          Open-source foundation with enterprise-grade multi-tenancy and verifiable guarantees.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-stretch">
        {tiers.map((tier) => (
          <div
            key={tier.name}
            className={`p-6 rounded-xl bg-zinc-950 flex flex-col justify-between relative border ${
              tier.highlight
                ? 'border-zinc-500 ring-1 ring-zinc-500/20'
                : 'border-zinc-800/80'
            }`}
          >
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <h3 className="text-sm font-bold text-white">{tier.name}</h3>
                {tier.highlight && (
                  <span className="px-1.5 py-0.2 rounded bg-white text-black text-[10px] font-medium">
                    Popular
                  </span>
                )}
              </div>
              <p className="text-zinc-400 text-xs mb-4 min-h-[32px]">{tier.desc}</p>
              <div className="flex items-baseline gap-1 mb-4">
                <span className="text-3xl font-bold text-white tracking-tight">{tier.price}</span>
                <span className="text-xs text-zinc-500">/{tier.period}</span>
              </div>
              <ul className="space-y-2 mb-6 text-xs text-zinc-300">
                {tier.features.map((feat) => (
                  <li key={feat} className="flex items-center gap-2">
                    <Check className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
                    <span>{feat}</span>
                  </li>
                ))}
              </ul>
            </div>
            <Link
              href={tier.href}
              className={`w-full py-2 rounded-lg text-center text-xs font-medium transition-colors ${
                tier.highlight
                  ? 'bg-white hover:bg-zinc-200 text-black'
                  : 'bg-zinc-900 hover:bg-zinc-800 text-zinc-200 border border-zinc-800'
              }`}
            >
              {tier.cta}
            </Link>
          </div>
        ))}
      </div>
    </section>
  );
}
