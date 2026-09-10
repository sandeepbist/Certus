'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { signIn } from '@/lib/auth-client';
import { Eye, EyeOff, Lock, Mail, ArrowRight, AlertCircle } from 'lucide-react';
import { OAuthButtons } from './OAuthButtons';

export function SignInForm({ providers }: { providers: Array<'google' | 'github'> }) {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const rememberMe = true;
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      const response = await signIn.email({
        email,
        password,
        rememberMe,
      });

      if (response?.error) {
        setError(response.error.message || 'Invalid email or password.');
      } else {
        router.push('/dashboard');
        router.refresh();
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
        <h1 className="text-lg font-semibold text-white tracking-tight">Sign In</h1>
        <p className="text-zinc-400 text-xs mt-0.5">Access your Certus workspace</p>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-zinc-900 border border-red-900/50 flex items-center gap-2 text-red-400 text-xs">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-3.5">
        {/* Email Field */}
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

        {/* Password Field */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="block text-xs text-zinc-300">Password</label>
            <Link
              href="/forgot-password"
              className="text-[11px] text-zinc-400 hover:text-white transition-colors"
            >
              Forgot?
            </Link>
          </div>
          <div className="relative">
            <Lock className="w-3.5 h-3.5 text-zinc-500 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type={showPassword ? 'text' : 'password'}
              required
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full pl-9 pr-8 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-white text-xs placeholder-zinc-500 focus:outline-none focus:border-zinc-500 transition-colors"
            />
            <button
              type="button"
              onClick={() => setShowPassword(!showPassword)}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
            >
              {showPassword ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        {/* Submit */}
        <button
          type="submit"
          disabled={isLoading}
          className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black font-medium text-xs transition-colors disabled:opacity-50 mt-1"
        >
          <span>{isLoading ? 'Authenticating...' : 'Sign In'}</span>
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

      {/* Footer */}
      <p className="mt-5 text-center text-xs text-zinc-400">
        Don&apos;t have an account?{' '}
        <Link href="/sign-up" className="text-white hover:underline font-medium">
          Create workspace
        </Link>
      </p>
    </div>
  );
}
