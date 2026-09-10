'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Mail, ArrowRight, CheckCircle2, AlertCircle } from 'lucide-react';
import { authClient } from '@/lib/auth-client';

export function ForgotPasswordForm() {
  const [email, setEmail] = useState('');
  const [isSubmitted, setIsSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      const response = await authClient.requestPasswordReset({
        email,
        redirectTo: '/reset-password',
      });
      if (response.error) {
        setError(response.error.message || 'Could not process the recovery request.');
        return;
      }
      setIsSubmitted(true);
    } catch {
      setError('Could not process request.');
    } finally {
      setIsLoading(false);
    }
  };

  if (isSubmitted) {
    return (
      <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 text-center">
        <div className="w-10 h-10 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-emerald-400 mx-auto mb-4">
          <CheckCircle2 className="w-5 h-5" />
        </div>
        <h1 className="text-lg font-semibold text-white tracking-tight mb-1">Check Your Inbox</h1>
        <p className="text-zinc-400 text-xs mb-6">
          If an account exists for <span className="text-white font-medium">{email}</span>, recovery
          instructions are ready. Local development writes the reset URL to the web server terminal.
        </p>
        <Link
          href="/sign-in"
          className="inline-flex items-center justify-center px-4 py-2 rounded-lg bg-zinc-900 hover:bg-zinc-850 text-zinc-200 border border-zinc-800 text-xs font-medium transition-colors"
        >
          Return to Sign In
        </Link>
      </div>
    );
  }

  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 relative">
      <div className="text-center mb-6">
        <h1 className="text-lg font-semibold text-white tracking-tight">Reset Password</h1>
        <p className="text-zinc-400 text-xs mt-0.5">
          Enter your email to receive recovery instructions
        </p>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-zinc-900 border border-red-900/50 flex items-center gap-2 text-red-400 text-xs">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-3.5">
        <div>
          <label className="block text-xs text-zinc-300 mb-1">Email</label>
          <div className="relative">
            <Mail className="w-3.5 h-3.5 text-zinc-500 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="email"
              required
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
          </div>
        </div>

        <button
          type="submit"
          disabled={isLoading}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors disabled:opacity-50 mt-1"
        >
          <span>{isLoading ? 'Sending...' : 'Send Recovery Link'}</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </button>
      </form>

      <p className="mt-5 text-center text-xs text-zinc-400">
        Remembered password?{' '}
        <Link href="/sign-in" className="text-white hover:underline font-medium">
          Sign in
        </Link>
      </p>
    </div>
  );
}
