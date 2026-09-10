'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { authClient } from '@/lib/auth-client';
import { ShieldCheck, ArrowRight, AlertCircle, KeyRound } from 'lucide-react';

export function TwoFactorForm() {
  const router = useRouter();
  const [code, setCode] = useState('');
  const [isBackup, setIsBackup] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      const response = isBackup
        ? await authClient.twoFactor.verifyBackupCode({ code, trustDevice: true })
        : await authClient.twoFactor.verifyTotp({ code, trustDevice: true });

      if (response.error) {
        setError(response.error.message || 'Invalid verification code.');
        return;
      }

      router.push('/dashboard');
      router.refresh();
    } catch {
      setError('Could not verify the code. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 text-center">
      <div className="w-10 h-10 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300 mx-auto mb-4">
        <ShieldCheck className="w-5 h-5" />
      </div>

      <h1 className="text-lg font-semibold text-white tracking-tight mb-1">Two-Factor Authentication</h1>
      <p className="text-zinc-400 text-xs mb-6">
        {isBackup
          ? 'Enter your 10-character backup code.'
          : 'Enter the 6-digit verification code from your authenticator app.'}
      </p>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-zinc-900 border border-red-900/50 flex items-center gap-2 text-red-400 text-xs text-left">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <input
            type="text"
            required
            autoFocus
            maxLength={isBackup ? 12 : 6}
            placeholder={isBackup ? 'XXXX-XXXX' : '000000'}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className="w-full py-2.5 px-3 text-center tracking-[0.25em] font-mono text-lg rounded-lg bg-zinc-900 border border-zinc-800 text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500 transition-colors"
          />
        </div>

        <button
          type="submit"
          disabled={isLoading || code.length < 4}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors disabled:opacity-50"
        >
          <span>{isLoading ? 'Verifying...' : 'Verify Code'}</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </button>
      </form>

      <div className="mt-5 pt-4 border-t border-zinc-850 flex items-center justify-between text-xs text-zinc-400">
        <button
          type="button"
          onClick={() => {
            setIsBackup(!isBackup);
            setCode('');
            setError(null);
          }}
          className="flex items-center gap-1 text-zinc-300 hover:text-white"
        >
          <KeyRound className="w-3 h-3" />
          <span>{isBackup ? 'Authenticator app' : 'Backup code'}</span>
        </button>

        <Link href="/sign-in" className="hover:text-white transition-colors">
          Sign In
        </Link>
      </div>
    </div>
  );
}
