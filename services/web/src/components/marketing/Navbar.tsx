'use client';

import Link from 'next/link';
import { ArrowRight } from 'lucide-react';

export function Navbar() {
  return (
    <header className="fixed top-0 left-0 right-0 z-40 h-14 border-b border-zinc-800/80 bg-black/90 backdrop-blur-md px-6 md:px-12 flex items-center justify-between">
      {/* Brand */}
      <Link href="/" className="flex items-center gap-2">
        <span className="font-semibold text-white tracking-tight text-sm">Certus</span>
        <span className="text-[10px] px-1.5 py-0.2 rounded bg-zinc-900 text-zinc-400 font-mono border border-zinc-800">
          v1.0
        </span>
      </Link>

      {/* Nav links */}
      <nav className="hidden md:flex items-center gap-6 text-xs font-medium text-zinc-400">
        <a href="#features" className="hover:text-white transition-colors">Capabilities</a>
        <a href="#architecture" className="hover:text-white transition-colors">Intent Engine</a>
        <a href="#pricing" className="hover:text-white transition-colors">Pricing</a>
      </nav>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <Link
          href="/sign-in"
          className="text-xs font-medium text-zinc-400 hover:text-white px-2 py-1 transition-colors"
        >
          Sign In
        </Link>
        <Link
          href="/dashboard"
          className="flex items-center gap-1 text-xs font-medium px-3 py-1.5 rounded-lg bg-white hover:bg-zinc-200 text-black transition-colors"
        >
          <span>Open Workspace</span>
          <ArrowRight className="w-3 h-3" />
        </Link>
      </div>
    </header>
  );
}
