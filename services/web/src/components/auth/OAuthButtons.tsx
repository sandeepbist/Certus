'use client';

import { signIn } from '@/lib/auth-client';
import { useState } from 'react';

type OAuthProvider = 'google' | 'github';

export function OAuthButtons({ providers }: { providers: OAuthProvider[] }) {
  const [loadingProvider, setLoadingProvider] = useState<string | null>(null);

  const handleOAuth = async (provider: OAuthProvider) => {
    try {
      setLoadingProvider(provider);
      await signIn.social({
        provider,
        callbackURL: '/dashboard',
      });
    } catch (err) {
      console.error(`OAuth error with ${provider}:`, err);
    } finally {
      setLoadingProvider(null);
    }
  };

  return (
    <div className={`grid gap-2 ${providers.length > 1 ? 'grid-cols-2' : 'grid-cols-1'}`}>
      {providers.map((provider) => (
        <button
          key={provider}
          type="button"
          onClick={() => handleOAuth(provider)}
          disabled={loadingProvider !== null}
          className="flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-zinc-900 hover:bg-zinc-850 border border-zinc-800 text-zinc-200 text-xs font-medium transition-colors disabled:opacity-50"
        >
          <span>
            {loadingProvider === provider
              ? 'Connecting...'
              : provider === 'google'
                ? 'Google'
                : 'GitHub'}
          </span>
        </button>
      ))}
    </div>
  );
}
