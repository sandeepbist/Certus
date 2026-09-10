'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Lock, ArrowRight, CheckCircle2, AlertCircle } from 'lucide-react';
import { authClient } from '@/lib/auth-client';

export default function ResetPasswordPage() {
  const router = useRouter();
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [isSuccess, setIsSuccess] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    if (password.length < 10) {
      setError('Password must be at least 10 characters long.');
      return;
    }

    setIsLoading(true);

    try {
      const token = new URLSearchParams(window.location.search).get('token');
      if (!token) {
        setError('This password reset link is invalid or expired.');
        return;
      }

      const response = await authClient.resetPassword({ newPassword: password, token });
      if (response.error) {
        setError(response.error.message || 'Could not update password.');
        return;
      }
      setIsSuccess(true);
      setTimeout(() => router.push('/sign-in'), 1500);
    } catch {
      setError('Could not update password.');
    } finally {
      setIsLoading(false);
    }
  };

  if (isSuccess) {
    return (
      <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 text-center">
        <div className="w-10 h-10 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-emerald-400 mx-auto mb-4">
          <CheckCircle2 className="w-5 h-5" />
        </div>
        <h1 className="text-lg font-semibold text-white tracking-tight mb-1">Password Updated</h1>
        <p className="text-zinc-400 text-xs mb-6">
          Your credentials have been updated. Redirecting to sign in...
        </p>
        <Link
          href="/sign-in"
          className="inline-flex items-center justify-center px-4 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium transition-colors"
        >
          Sign In Now
        </Link>
      </div>
    );
  }

  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 relative">
      <div className="text-center mb-6">
        <h1 className="text-lg font-semibold text-white tracking-tight">Set New Password</h1>
        <p className="text-zinc-400 text-xs mt-0.5">Enter your new account password</p>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-zinc-900 border border-red-900/50 flex items-center gap-2 text-red-400 text-xs">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-3.5">
        <div>
          <label className="block text-xs text-zinc-300 mb-1">New Password</label>
          <div className="relative">
            <Lock className="w-3.5 h-3.5 text-zinc-500 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="password"
              required
              minLength={10}
              maxLength={128}
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
          </div>
        </div>

        <div>
          <label className="block text-xs text-zinc-300 mb-1">Confirm New Password</label>
          <div className="relative">
            <Lock className="w-3.5 h-3.5 text-zinc-500 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="password"
              required
              minLength={10}
              maxLength={128}
              placeholder="••••••••"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
          </div>
        </div>

        <button
          type="submit"
          disabled={isLoading}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors disabled:opacity-50 mt-1"
        >
          <span>{isLoading ? 'Updating...' : 'Save New Password'}</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </button>
      </form>
    </div>
  );
}
