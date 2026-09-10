'use client';

import { useState } from 'react';
import Link from 'next/link';
import { MailCheck, ArrowRight, RefreshCw } from 'lucide-react';

export default function VerifyEmailPage() {
  const [isResent, setIsResent] = useState(false);
  const [isResending, setIsResending] = useState(false);

  const handleResend = async () => {
    setIsResending(true);
    await new Promise((resolve) => setTimeout(resolve, 600));
    setIsResent(true);
    setIsResending(false);
  };

  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 text-center">
      <div className="w-10 h-10 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300 mx-auto mb-4">
        <MailCheck className="w-5 h-5" />
      </div>

      <h1 className="text-lg font-semibold text-white tracking-tight mb-1">Verify Your Email</h1>
      <p className="text-zinc-400 text-xs mb-6 leading-relaxed">
        We sent an activation link to your email address. Click the link to verify your identity.
      </p>

      {isResent && (
        <div className="mb-4 p-2.5 rounded-lg bg-zinc-900 border border-zinc-800 text-emerald-400 text-xs">
          A fresh verification link has been dispatched.
        </div>
      )}

      <div className="space-y-2.5">
        <Link
          href="/dashboard"
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors"
        >
          <span>Continue to Dashboard</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </Link>

        <button
          type="button"
          onClick={handleResend}
          disabled={isResending}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-zinc-900 hover:bg-zinc-850 text-zinc-300 border border-zinc-800 text-xs font-medium transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`w-3 h-3 ${isResending ? 'animate-spin' : ''}`} />
          <span>{isResending ? 'Sending...' : 'Resend Email'}</span>
        </button>
      </div>

      <p className="mt-6 text-center text-xs text-zinc-500">
        Wrong email?{' '}
        <Link href="/sign-up" className="text-white hover:underline font-medium">
          Sign up with different address
        </Link>
      </p>
    </div>
  );
}
