'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { signUp } from '@/lib/auth-client';
import { createWorkspace } from '@/lib/workspace-client';
import { Lock, Mail, User, ArrowRight, AlertCircle } from 'lucide-react';
import { OAuthButtons } from './OAuthButtons';

export function SignUpForm({ providers }: { providers: Array<'google' | 'github'> }) {
  const router = useRouter();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const hasMinLength = password.length >= 10;
  const hasNumber = /\d/.test(password);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    if (!hasMinLength || !hasNumber) {
      setError('Password must be at least 10 characters with at least one number.');
      return;
    }

    if (!acceptedTerms) {
      setError('Please accept terms to continue.');
      return;
    }

    setIsLoading(true);

    try {
      const response = await signUp.email({
        email,
        password,
        name,
      });

      if (response?.error) {
        setError(response.error.message || 'Could not complete registration.');
      } else {
        try {
          await createWorkspace(`${name.trim()}'s Workspace`);
          router.push('/dashboard');
          router.refresh();
        } catch {
          router.push('/onboarding');
          router.refresh();
        }
      }
    } catch {
      setError('Could not reach the authentication service. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="w-full max-w-sm p-6 rounded-2xl bg-zinc-950 border border-zinc-800/80 relative">
      <div className="text-center mb-6">
        <h1 className="text-lg font-semibold text-white tracking-tight">Create Workspace</h1>
        <p className="text-zinc-400 text-xs mt-0.5">Deploy your AI-OS workspace</p>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-zinc-900 border border-red-900/50 flex items-center gap-2 text-red-400 text-xs">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-3">
        {/* Full Name */}
        <div>
          <label className="block text-xs text-zinc-300 mb-1">Full Name</label>
          <div className="relative">
            <User className="w-3.5 h-3.5 text-zinc-500 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              required
              placeholder="Alex Vance"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
          </div>
        </div>

        {/* Email */}
        <div>
          <label className="block text-xs text-zinc-300 mb-1">Email</label>
          <div className="relative">
            <Mail className="w-3.5 h-3.5 text-zinc-500 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="email"
              required
              placeholder="name@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
          </div>
        </div>

        {/* Password */}
        <div>
          <label className="block text-xs text-zinc-300 mb-1">Password</label>
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

        {/* Confirm Password */}
        <div>
          <label className="block text-xs text-zinc-300 mb-1">Confirm Password</label>
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

        {/* Terms */}
        <div className="flex items-start pt-1">
          <input
            id="terms"
            type="checkbox"
            checked={acceptedTerms}
            onChange={(e) => setAcceptedTerms(e.target.checked)}
            className="w-3.5 h-3.5 mt-0.5 rounded bg-zinc-900 border-zinc-700 text-white"
          />
          <label htmlFor="terms" className="ml-2 text-xs text-zinc-400 cursor-pointer">
            I accept the Terms of Service & Privacy Policy
          </label>
        </div>

        {/* Submit */}
        <button
          type="submit"
          disabled={isLoading}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors disabled:opacity-50 mt-2"
        >
          <span>{isLoading ? 'Creating Account...' : 'Get Started Free'}</span>
          <ArrowRight className="w-3.5 h-3.5" />
        </button>
      </form>

      {providers.length > 0 && (
        <>
          <div className="relative my-4 text-center">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-zinc-850"></div>
            </div>
            <span className="relative px-2 bg-zinc-950 text-[10px] text-zinc-500 uppercase font-mono">
              or
            </span>
          </div>
          <OAuthButtons providers={providers} />
        </>
      )}

      <p className="mt-5 text-center text-xs text-zinc-400">
        Already have an account?{' '}
        <Link href="/sign-in" className="text-white hover:underline font-medium">
          Sign in
        </Link>
      </p>
    </div>
  );
}
