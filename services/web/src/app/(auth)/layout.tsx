import Link from 'next/link';

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen bg-black flex flex-col justify-between p-6 relative overflow-hidden text-zinc-100">
      {/* Header with clean brand */}
      <div className="flex items-center justify-between max-w-5xl w-full mx-auto">
        <Link href="/" className="flex items-center gap-2">
          <span className="font-semibold text-white tracking-tight text-sm">Certus</span>
          <span className="text-[10px] px-1.5 py-0.2 rounded bg-zinc-900 text-zinc-400 font-mono border border-zinc-800">
            Auth
          </span>
        </Link>
      </div>

      {/* Auth Content */}
      <div className="my-auto flex items-center justify-center py-10">
        {children}
      </div>

      {/* Footer */}
      <div className="text-center text-xs text-zinc-600 font-mono">
        <p>© {new Date().getFullYear()} Certus • Continuous Intent Assurance</p>
      </div>
    </div>
  );
}
