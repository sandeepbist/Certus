import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';

const databaseUrl = process.env.CERTUS_TEST_DATABASE_URL;
assert.ok(databaseUrl, 'Set CERTUS_TEST_DATABASE_URL to a disposable migrated database.');
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(databaseUrl).hostname));
Object.assign(process.env, {
  DATABASE_URL: databaseUrl,
  NODE_ENV: 'test',
  BETTER_AUTH_SECRET: 'certus-disposable-auth-database-check-secret',
  BETTER_AUTH_URL: 'http://localhost:3000',
  APP_URL: 'http://localhost:3000',
  GOOGLE_CLIENT_ID: '',
  GOOGLE_CLIENT_SECRET: '',
  GITHUB_CLIENT_ID: '',
  GITHUB_CLIENT_SECRET: '',
});

const { getAuth } = await import('../services/web/src/lib/auth');
const { getDb, closeDbPool } = await import('../services/web/src/lib/db');
const auth = getAuth();
const fixtureId = randomUUID();
const email = `auth-check-${fixtureId}@example.invalid`;
const password = 'certus-disposable-auth-check-password';
const cookies = new Map<string, string>();
const headers = () => new Headers({
  origin: 'http://localhost:3000',
  cookie: [...cookies].map(([name, value]) => `${name}=${value}`).join('; '),
});
function rememberCookies(responseHeaders: Headers) {
  for (const cookie of responseHeaders.getSetCookie()) {
    const pair = cookie.split(';', 1)[0];
    const separator = pair.indexOf('=');
    cookies.set(pair.slice(0, separator), pair.slice(separator + 1));
  }
}

let userId: string | undefined;
let organizationId: string | undefined;
let keyId: string | undefined;
try {
  const signedUp = await auth.api.signUpEmail({
    body: { email, password, name: 'Auth database check' },
    headers: headers(), returnHeaders: true,
  });
  userId = signedUp.response.user.id;
  rememberCookies(signedUp.headers);
  assert.equal((await auth.api.getSession({ headers: headers() }))?.user.id, userId);
  const account = await getDb().query('SELECT issuer FROM "account" WHERE "userId" = $1', [userId]);
  assert.equal(account.rows[0].issuer, null);
  await getDb().query('UPDATE "account" SET issuer = $1 WHERE "userId" = $2', ['legacy-issuer', userId]);
  await assert.rejects(getDb().query(
    `INSERT INTO "account" (id, issuer, "accountId", "providerId", "userId", "createdAt", "updatedAt")
     SELECT $1, $2, "accountId", "providerId", "userId", NOW(), NOW()
     FROM "account" WHERE "userId" = $3`,
    [randomUUID(), 'different-issuer', userId],
  ), { code: '23505', constraint: 'account_providerId_accountId_uidx' });

  const organization = await auth.api.createOrganization({
    body: { name: 'Auth database check', slug: `auth-check-${fixtureId}` },
    headers: headers(), returnHeaders: true,
  });
  organizationId = organization.response.id;
  rememberCookies(organization.headers);
  const activated = await auth.api.setActiveOrganization({
    body: { organizationId }, headers: headers(), returnHeaders: true,
  });
  rememberCookies(activated.headers);
  assert.equal((await auth.api.getSession({ headers: headers() }))?.session.activeOrganizationId, organizationId);
  const preferences = await getDb().query('SELECT organization_id FROM user_preferences WHERE user_id = $1', [userId]);
  assert.equal(preferences.rows[0].organization_id, organizationId);
  const key = await auth.api.createApiKey({ headers: headers(), body: {
    configId: 'certus-workspace', name: 'Auth database check', organizationId,
    expiresIn: 30 * 24 * 60 * 60,
  } });
  keyId = key.id;
  await getDb().query(
    'INSERT INTO certus_api_keys (key_id, tenant_id, created_by, scopes) VALUES ($1, $2, $3, $4)',
    [key.id, organizationId, userId, ['read']],
  );
  assert.equal((await auth.api.verifyApiKey({ body: { key: key.key, configId: 'certus-workspace' } })).valid, true);
  const sessionHeaders = headers();
  await auth.api.signOut({ headers: sessionHeaders });
  assert.equal(await auth.api.getSession({ headers: sessionHeaders }), null);
  await assert.rejects(auth.api.signInEmail({ body: { email, password: 'incorrect-password' } }), { status: 'UNAUTHORIZED' });
  const signedIn = await auth.api.signInEmail({ body: { email, password } });
  assert.equal(signedIn.user.id, userId);
  const retained = await getDb().query('SELECT issuer FROM "account" WHERE "userId" = $1', [userId]);
  assert.equal(retained.rows[0].issuer, 'legacy-issuer');
} finally {
  try {
    if (keyId) {
      await getDb().query('DELETE FROM apikey WHERE id = $1', [keyId]);
      const remaining = await getDb().query('SELECT id FROM apikey WHERE id = $1', [keyId]);
      assert.equal(remaining.rowCount, 0);
    }
    if (organizationId) await getDb().query('DELETE FROM "organization" WHERE id = $1', [organizationId]);
    if (userId) await getDb().query('DELETE FROM "user" WHERE id = $1', [userId]);
  } finally {
    await closeDbPool();
  }
}
console.log('Auth database check passed: signup, legacy signin, identity uniqueness, sessions, workspace hook, API key, password rejection, and fixture cleanup.');
