import { createAuthClient } from 'better-auth/react';
import { organizationClient, twoFactorClient } from 'better-auth/client/plugins';
import { passkeyClient } from '@better-auth/passkey/client';
import { apiKeyClient } from '@better-auth/api-key/client';

export const authClient = createAuthClient({
  baseURL: typeof window !== 'undefined' ? window.location.origin : process.env.BETTER_AUTH_URL || 'http://localhost:3000',
  plugins: [
    organizationClient(),
    twoFactorClient({
      twoFactorPage: '/two-factor',
    }),
    passkeyClient(),
    apiKeyClient(),
  ],
});

export const {
  signIn,
  signOut,
  signUp,
  useSession,
} = authClient;
