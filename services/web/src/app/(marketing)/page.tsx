import Link from 'next/link';
import { Hero } from '@/components/marketing/Hero';
import { FeatureGrid } from '@/components/marketing/FeatureGrid';
import { HowItWorks } from '@/components/marketing/HowItWorks';
import { PricingPreview } from '@/components/marketing/PricingPreview';
import { ArrowRight } from 'lucide-react';

export default function LandingPage() {
  return (
    <>
      <Hero />
      <FeatureGrid />
      <HowItWorks />
      <PricingPreview />

      {/* Call To Action Section */}
      <section className="py-20 px-6 md:px-12 max-w-5xl mx-auto text-center">
        <div className="p-10 sm:p-14 rounded-2xl bg-zinc-950 border border-zinc-800/80">
          <h2 className="text-2xl sm:text-4xl font-bold text-white tracking-tight mb-3 max-w-xl mx-auto">
            Ready to Build With Certus?
          </h2>
          <p className="text-zinc-400 text-xs sm:text-sm max-w-lg mx-auto mb-6">
            Connect your enterprise knowledge and let autonomous agents execute complex workflows with continuous intent assurance.
          </p>
          <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
            <Link
              href="/dashboard"
              className="flex items-center gap-1.5 px-6 py-2.5 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors"
            >
              <span>Launch Workspace</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </Link>
            <Link
              href="/sign-in"
              className="px-5 py-2.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border border-zinc-800 text-xs font-medium transition-colors"
            >
              Sign In to Workspace
            </Link>
          </div>
        </div>
      </section>
    </>
  );
}
