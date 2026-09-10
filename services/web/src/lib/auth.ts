import { betterAuth } from 'better-auth';
import { organization, twoFactor } from 'better-auth/plugins';
import { passkey } from '@better-auth/passkey';
import { apiKey } from '@better-auth/api-key';
import { getDb } from './db';

function createAuth() {
  const isProduction = process.env.NODE_ENV === 'production';
  const authSecret = process.env.BETTER_AUTH_SECRET?.trim();

  if (!authSecret) {
    throw new Error(
      'BETTER_AUTH_SECRET is required. Run `bun run setup` to generate local configuration.',
    );
  }

  const googleClientId = process.env.GOOGLE_CLIENT_ID?.trim();
  const googleClientSecret = process.env.GOOGLE_CLIENT_SECRET?.trim();
  const githubClientId = process.env.GITHUB_CLIENT_ID?.trim();
  const githubClientSecret = process.env.GITHUB_CLIENT_SECRET?.trim();
  const db = getDb();

  return betterAuth({
    appName: 'Certus',
    baseURL: process.env.BETTER_AUTH_URL || 'http://localhost:3000',
    secret: authSecret,
    trustedOrigins: [process.env.APP_URL || 'http://localhost:3000'],
    database: db,
    emailAndPassword: {
      enabled: true,
      requireEmailVerification: false,
      minPasswordLength: 10,
      maxPasswordLength: 128,
      autoSignIn: true,
      ...(!isProduction
        ? {
            sendResetPassword: async ({ user, url }: { user: { email: string }; url: string }) => {
              console.info(`[Certus local email] Password reset for ${user.email}: ${url}`);
            },
          }
        : {}),
      revokeSessionsOnPasswordReset: true,
    },
    socialProviders: {
      ...(googleClientId && googleClientSecret
        ? { google: { clientId: googleClientId, clientSecret: googleClientSecret } }
        : {}),
      ...(githubClientId && githubClientSecret
        ? { github: { clientId: githubClientId, clientSecret: githubClientSecret } }
        : {}),
    },
    plugins: [
      organization({
        allowUserToCreateOrganization: true,
        organizationHooks: {
          afterCreateOrganization: async ({ organization: createdOrganization, user }) => {
            await db.query(
              `WITH ensured_tenant AS (
                 INSERT INTO tenant_config (organization_id)
                 VALUES ($1)
                 ON CONFLICT (organization_id) DO UPDATE
                 SET organization_id = EXCLUDED.organization_id
                 RETURNING organization_id
               )
               INSERT INTO user_preferences (user_id, organization_id)
               SELECT $2, organization_id FROM ensured_tenant
               ON CONFLICT (user_id) DO UPDATE
               SET organization_id = EXCLUDED.organization_id, updated_at = NOW()`,
              [createdOrganization.id, user.id],
            );
          },
        },
      }),
      twoFactor({
        issuer: 'Certus',
      }),
      passkey(),
      apiKey({
        configId: 'certus-workspace',
        defaultPrefix: 'certus_',
        references: 'organization',
        requireName: true,
        enableSessionForAPIKeys: false,
        keyExpiration: {
          defaultExpiresIn: 90 * 24 * 60 * 60 * 1000,
          maxExpiresIn: 365,
        },
        rateLimit: {
          enabled: true,
          timeWindow: 60 * 60 * 1000,
          maxRequests: 1_000,
        },
      }),
    ],
    session: {
      expiresIn: 60 * 60 * 24 * 7, // 7 days
      updateAge: 60 * 60 * 24, // Refresh every 24h
    },
    advanced: {
      database: {
        joins: true,
      },
      cookiePrefix: 'certus',
      useSecureCookies: isProduction,
      crossSubDomainCookies: {
        enabled: false,
      },
    },
  });
}

type CertusAuth = ReturnType<typeof createAuth>;

const globalForAuth = globalThis as typeof globalThis & {
  certusAuth?: CertusAuth;
};

export function getAuth(): CertusAuth {
  if (!globalForAuth.certusAuth) {
    globalForAuth.certusAuth = createAuth();
  }
  return globalForAuth.certusAuth;
}
