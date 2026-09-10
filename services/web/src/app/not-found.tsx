import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="min-h-screen bg-black flex flex-col items-center justify-center p-6 text-center text-white">
      <div className="w-12 h-12 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-400 font-mono text-lg font-semibold mb-4">
        404
      </div>
      <h1 className="text-xl font-semibold mb-2 tracking-tight">Page Not Found</h1>
      <p className="text-zinc-400 text-xs max-w-md mb-6">
        The system could not locate the requested resource.
      </p>
      <Link
        href="/dashboard"
        className="px-4 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium transition-colors"
      >
        Return to Workspace
      </Link>
    </div>
  );
}
