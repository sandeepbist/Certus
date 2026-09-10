'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowRight, AlertCircle, Building2 } from 'lucide-react';
import { createWorkspace } from '@/lib/workspace-client';

export function WorkspaceSetupForm({ defaultName }: { defaultName: string }) {
  const router = useRouter();
  const [name, setName] = useState(defaultName);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      await createWorkspace(name);
      router.push('/dashboard');
      router.refresh();
    } catch (caughtError) {
      setError(
        caughtError instanceof Error ? caughtError.message : 'Could not create the workspace.',
      );
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80">
      <div className="w-10 h-10 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300 mx-auto mb-4">
        <Building2 className="w-5 h-5" />
      </div>
      <div className="text-center mb-6">
        <h1 className="text-lg font-semibold text-white tracking-tight">Create your workspace</h1>
        <p className="text-zinc-400 text-xs mt-1">
          Your documents, agents, tasks, and usage stay isolated inside this workspace.
        </p>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-zinc-900 border border-red-900/50 flex items-center gap-2 text-red-400 text-xs">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="workspace-name" className="block text-xs text-zinc-300 mb-1">
            Workspace name
          </label>
          <input
            id="workspace-name"
            type="text"
            required
            minLength={2}
            maxLength={80}
            value={name}
            onChange={(event) => setName(event.target.value)}
            className="w-full px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
          />
        </div>

        <button
          type="submit"
          disabled={isLoading}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors disabled:opacity-50"
        >
          <span>{isLoading ? 'Creating workspace...' : 'Continue to Certus'}</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </button>
      </form>
    </div>
  );
}
