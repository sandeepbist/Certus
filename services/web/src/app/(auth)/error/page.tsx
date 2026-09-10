import Link from 'next/link';
import { AlertTriangle, ArrowRight } from 'lucide-react';

export default function AuthErrorPage() {
  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 text-center">
      <div className="w-10 h-10 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-red-400 mx-auto mb-4">
        <AlertTriangle className="w-5 h-5" />
      </div>

      <h1 className="text-lg font-semibold text-white tracking-tight mb-1">Authentication Error</h1>
      <p className="text-zinc-400 text-xs mb-6 leading-relaxed">
        Could not complete sign in. The session token may have expired or provider returned an error.
      </p>

      <div className="space-y-2.5">
        <Link
          href="/sign-in"
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors"
        >
          <span>Try Again</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </Link>

        <Link
          href="/"
          className="w-full inline-block py-2 rounded-lg bg-zinc-900 hover:bg-zinc-850 text-zinc-300 border border-zinc-800 text-xs font-medium transition-colors"
        >
          Return Home
        </Link>
      </div>
    </div>
  );
}
