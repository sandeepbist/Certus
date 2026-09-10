'use client';

import { useEffect } from 'react';

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('App error:', error);
  }, [error]);

  return (
    <div className="min-h-screen bg-black flex flex-col items-center justify-center p-6 text-center text-white">
      <div className="w-12 h-12 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-red-400 font-mono text-xl font-bold mb-4">
        !
      </div>
      <h1 className="text-xl font-semibold mb-2 tracking-tight">System Encountered an Error</h1>
      <p className="text-zinc-400 text-xs max-w-md mb-6">
        {error.message || 'An unexpected error occurred during execution.'}
      </p>
      <button
        onClick={() => reset()}
        className="px-4 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium transition-colors"
      >
        Retry Action
      </button>
    </div>
  );
}
