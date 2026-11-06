'use client';

import { useState } from 'react';
import Link from 'next/link';
import { authClient } from '@/lib/auth-client';

export function VerifyEmailForm({ verified, invalid }: { verified: boolean; invalid: boolean }) {
  const [email, setEmail] = useState('');
  const [state, setState] = useState<'idle' | 'sending' | 'submitted'>('idle');
  const [error, setError] = useState<string | null>(null);

  async function resend(event: React.FormEvent) {
    event.preventDefault();
    setState('sending');
    setError(null);
    try {
      const response = await authClient.sendVerificationEmail({ email, callbackURL: '/verify-email?verified=1' });
      if (response.error) {
        setError(response.error.message || 'Could not request verification email. Please try later.');
        setState('idle');
      } else {
        setState('submitted');
      }
    } catch {
      setError('Could not reach the authentication service. Please try later.');
      setState('idle');
    }
  }

  return <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800 text-center space-y-4">
    <h1 className="text-lg font-semibold">{verified ? 'Email verified' : 'Verify your email'}</h1>
    {verified ? <>
      <p className="text-xs text-zinc-400">Sign in to continue to your workspace.</p>
      <Link href="/sign-in" className="block underline">Continue to sign in</Link>
    </> : <>
      <p className="text-xs text-zinc-400">Open the verification link in your email. Enter your email below to request another link.</p>
      {invalid && <p role="alert" className="text-xs text-red-400">This verification link is invalid or expired. Request a new link.</p>}
      {error && <p role="alert" className="text-xs text-red-400">{error}</p>}
      {state === 'submitted' && <p role="status" className="text-xs text-zinc-400">If this account needs verification, email delivery was requested. This does not confirm delivery. If no email arrives, retry later or contact the operator.</p>}
      <form onSubmit={resend} className="space-y-3 text-left">
        <label htmlFor="verification-email" className="block text-xs">Email</label>
        <input id="verification-email" type="email" autoComplete="email" required value={email}
          onChange={event => setEmail(event.target.value)}
          className="w-full p-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs" />
        <button disabled={state === 'sending'} className="w-full p-2 rounded-lg bg-white text-black text-xs disabled:opacity-50">
          {state === 'sending' ? 'Requesting...' : 'Resend verification email'}
        </button>
      </form>
    </>}
    <Link href="/sign-in" className="block text-xs underline">Back to sign in</Link>
  </div>;
}
