import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createEmailVerificationToken } from 'better-auth/api';
import { createAuthMail } from '../services/web/src/lib/auth-mail';

const databaseUrl = process.env.CERTUS_TEST_DATABASE_URL;
const sinkUrl = process.env.CERTUS_TEST_MAILPIT_URL;
assert.ok(databaseUrl && sinkUrl, 'Set disposable CERTUS_TEST_DATABASE_URL and CERTUS_TEST_MAILPIT_URL.');
for (const value of [databaseUrl, sinkUrl]) {
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(value).hostname), 'Fixtures must be local.');
}
assert.equal(process.env.AUTH_MAIL_MODE, 'smtp');
Object.assign(process.env, {
  DATABASE_URL: databaseUrl, NODE_ENV: 'test',
  BETTER_AUTH_SECRET: 'certus-disposable-email-check-secret',
  BETTER_AUTH_URL: 'http://localhost:3000', APP_URL: 'http://localhost:3000',
  GOOGLE_CLIENT_ID: '', GOOGLE_CLIENT_SECRET: '', GITHUB_CLIENT_ID: '', GITHUB_CLIENT_SECRET: '',
});
const failureMode = process.argv[2];
if (failureMode === '--disabled') process.env.AUTH_MAIL_MODE = 'disabled';
if (failureMode === '--offline') process.env.SMTP_PORT = '1';

const { getAuth } = await import('../services/web/src/lib/auth');
const { getDb, closeDbPool } = await import('../services/web/src/lib/db');
const auth = getAuth();
if (failureMode === '--disabled' || failureMode === '--offline') {
  try {
    for (const email of [process.env.CERTUS_EMAIL_CHECK_ACCOUNT, 'unknown@example.invalid']) {
      for (const path of ['/sign-up/email', '/send-verification-email', '/request-password-reset']) {
        const response = await auth.handler(new Request(`http://localhost:3000/api/auth${path}`, {
          method: 'POST', headers: { origin: 'http://localhost:3000', 'content-type': 'application/json' },
          body: JSON.stringify({ email, password: 'disposable-password-123', name: 'Unavailable check' }),
        }));
        assert.equal(response.status, 503);
        assert.equal((await response.json()).code, 'AUTH_MAIL_UNAVAILABLE');
      }
    }
    if (process.env.CERTUS_EMAIL_CHECK_ACCOUNT) {
      const signin = await auth.handler(new Request('http://localhost:3000/api/auth/sign-in/email', {
        method: 'POST', headers: { origin: 'http://localhost:3000', 'content-type': 'application/json' },
        body: JSON.stringify({ email: process.env.CERTUS_EMAIL_CHECK_ACCOUNT, password: 'disposable-old-password-123' }),
      }));
      assert.equal(signin.status, 200, 'Verified local users can sign in when email is unavailable');
    }
    console.log('Unavailable endpoint proof passed.');
  } finally { await closeDbPool(); }
  process.exit(0);
}
const id = randomUUID();
const email = `email-check-${id}@example.invalid`;
const unknownEmail = `unknown-${id}@example.invalid`;
const password = 'disposable-old-password-123';
const newPassword = 'disposable-new-password-456';
const messages = new Set<string>();
let userId: string | undefined;
let requestNumber = 0;
let sessionCookie = '';

async function call(path: string, body?: object, cookie = '', ip?: string) {
  const response = await auth.handler(new Request(`http://localhost:3000/api/auth${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { origin: 'http://localhost:3000', 'content-type': 'application/json', cookie,
      'x-forwarded-for': ip || `192.0.2.${++requestNumber}` },
    ...(body ? { body: JSON.stringify(body) } : {}),
  }));
  return response;
}
async function inbox() {
  const response = await fetch(`${sinkUrl}/api/v1/search?query=${encodeURIComponent(`to:${email}`)}`);
  assert.equal(response.status, 200);
  const data: { messages: { ID: string }[] } = await response.json();
  return data.messages;
}
async function deliveredLink(kind: 'verification' | 'reset') {
  const received = await inbox();
  for (const message of received) {
    messages.add(message.ID);
    const response = await fetch(`${sinkUrl}/api/v1/message/${message.ID}`);
    const data: { Text: string } = await response.json();
    const link = data.Text.match(/http:\/\/localhost:3000\/api\/auth\/\S+/)?.[0];
    if (link && link.includes(kind === 'verification' ? '/verify-email?' : '/reset-password/')) return new URL(link);
  }
  assert.fail('Expected a real SMTP message in the disposable sink.');
}
async function removeMessages() {
  for (const { ID } of await inbox()) messages.add(ID);
  for (const ID of messages) {
    const response = await fetch(`${sinkUrl}/api/v1/messages`, {
      method: 'DELETE', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ IDs: [ID] }),
    });
    assert.equal(response.ok, true, 'Fixture message cleanup');
  }
  messages.clear();
}
async function resetLink() {
  await removeMessages();
  const response = await call('/request-password-reset', { email, redirectTo: '/reset-password' });
  assert.equal(response.status, 200);
  return deliveredLink('reset');
}
function cookie(response: Response) {
  return response.headers.getSetCookie().map(value => value.split(';', 1)[0]).join('; ');
}

try {
  const signup = await call('/sign-up/email', { email, password, name: 'Email check', callbackURL: '/verify-email?verified=1' });
  assert.equal(signup.status, 200, 'Signup succeeds with configured SMTP');
  const signedUp: { token: string | null; user: { id: string; emailVerified: boolean } } = await signup.json();
  userId = signedUp.user.id;
  assert.equal(signedUp.token, null);
  assert.equal(signedUp.user.emailVerified, false);
  assert.equal(cookie(signup), '');
  assert.equal((await call('/organization/create', { name: 'Forbidden', slug: `forbidden-${id}` })).status, 401);
  const unverified = await call('/sign-in/email', { email, password });
  assert.equal(unverified.status, 403);
  assert.equal((await unverified.json()).code, 'EMAIL_NOT_VERIFIED');
  const initial = await deliveredLink('verification');
  const payload: { exp: number; iat: number } = JSON.parse(Buffer.from(initial.searchParams.get('token')?.split('.')[1] || '', 'base64url').toString());
  assert.equal(payload.exp - payload.iat, 24 * 60 * 60, 'Delivered verification lifetime matches the PRD');
  const hostile = new URL(initial);
  hostile.searchParams.set('callbackURL', 'https://attacker.invalid/');
  assert.equal((await call(`${hostile.pathname.replace('/api/auth', '')}${hostile.search}`)).status, 403);
  const invalid = await call('/verify-email?token=invalid');
  assert.equal(invalid.status, 401);
  assert.equal(auth.options.emailVerification?.expiresIn, 24 * 60 * 60);
  assert.equal(auth.options.emailAndPassword?.resetPasswordTokenExpiresIn, 60 * 60);
  const expired = await createEmailVerificationToken('certus-disposable-email-check-secret', email, undefined, -60);
  assert.equal((await call(`/verify-email?token=${expired}`)).status, 401);

  await removeMessages();
  const resend = await call('/send-verification-email', { email, callbackURL: '/verify-email?verified=1' });
  const unknown = await call('/send-verification-email', { email: unknownEmail, callbackURL: '/verify-email?verified=1' });
  assert.equal(resend.status, 200);
  assert.deepEqual(await unknown.json(), await resend.json(), 'Resend is generic for unknown accounts');
  const verification = await deliveredLink('verification');
  const verified = await call(`${verification.pathname.replace('/api/auth', '')}${verification.search}`);
  assert.equal(verified.status, 302);
  assert.equal(cookie(verified), '', 'Verification does not sign in or bypass 2FA');
  assert.ok(verified.headers.get('location')?.includes('verified=1'));
  const replay = await call(`${verification.pathname.replace('/api/auth', '')}${verification.search}`);
  assert.equal(replay.status, 302);
  assert.equal(cookie(replay), '', 'Reused verification token cannot authenticate');
  const verifiedUser = await getDb().query('SELECT "emailVerified" FROM "user" WHERE id = $1', [userId]);
  assert.equal(verifiedUser.rows[0].emailVerified, true);
  const signin = await call('/sign-in/email', { email, password });
  assert.equal(signin.status, 200);
  sessionCookie = cookie(signin);
  assert.ok(sessionCookie);
  assert.equal((await call('/get-session', undefined, sessionCookie)).status, 200);

  await getDb().query('UPDATE "user" SET "emailVerified" = false WHERE id = $1', [userId]);
  assert.equal((await call('/organization/create', { name: 'Blocked legacy session', slug: `legacy-${id}` }, sessionCookie)).status, 403);
  await getDb().query('UPDATE "user" SET "emailVerified" = true WHERE id = $1', [userId]);

  const link = await resetLink();
  const redirect = await call(`${link.pathname.replace('/api/auth', '')}${link.search}`);
  assert.equal(redirect.status, 302);
  const landing = new URL(redirect.headers.get('location') || '', 'http://localhost:3000');
  const token = landing.searchParams.get('token');
  assert.ok(token);
  const resetRows = await getDb().query('SELECT EXTRACT(EPOCH FROM ("expiresAt" - NOW())) AS seconds FROM verification WHERE value = $1', [userId]);
  assert.ok(Number(resetRows.rows[0].seconds) > 3590 && Number(resetRows.rows[0].seconds) <= 3600, 'Delivered reset lifetime matches the PRD');
  const genericReset = await call('/request-password-reset', { email: unknownEmail, redirectTo: '/reset-password' });
  const knownReset = await call('/request-password-reset', { email, redirectTo: '/reset-password' });
  assert.deepEqual(await genericReset.json(), await knownReset.json());
  const hostileReset = await call('/request-password-reset', { email, redirectTo: 'https://attacker.invalid/' });
  assert.equal(hostileReset.status, 403);
  assert.equal((await call('/reset-password', { token, newPassword: 'short' })).status, 400);
  assert.equal((await call('/reset-password', { token: 'invalid', newPassword })).status, 400);
  assert.equal((await call('/reset-password', { token, newPassword })).status, 200);
  assert.equal((await call('/reset-password', { token, newPassword: password })).status, 400);
  assert.equal(await (await call('/get-session', undefined, sessionCookie)).json(), null, 'Reset revokes existing session');
  assert.equal((await call('/sign-in/email', { email, password })).status, 401);
  assert.equal((await call('/sign-in/email', { email, password: newPassword })).status, 200);

  const expiredReset = await resetLink();
  const resetToken = expiredReset.pathname.split('/').at(-1);
  await getDb().query('UPDATE verification SET "expiresAt" = NOW() - INTERVAL \'1 minute\' WHERE value = $1', [userId]);
  assert.equal((await call('/reset-password', { token: resetToken, newPassword: password })).status, 400);
  for (let attempt = 0; attempt < 3; attempt++) {
    assert.equal((await call('/send-verification-email', { email: unknownEmail }, '', '198.51.100.42')).status, 200);
  }
  assert.equal((await call('/send-verification-email', { email: unknownEmail }, '', '198.51.100.42')).status, 429);

  const concurrentLink = await resetLink();
  const concurrentToken = concurrentLink.pathname.split('/').at(-1);
  const concurrent = await Promise.all([
    call('/reset-password', { token: concurrentToken, newPassword: password }),
    call('/reset-password', { token: concurrentToken, newPassword: password }),
  ]);
  assert.deepEqual(concurrent.map(response => response.status).sort(), [200, 400], 'Only one concurrent reset consumes the token');
  for (const mode of ['--disabled', '--offline']) {
    const child = Bun.spawn([process.execPath, 'run', import.meta.path, mode], { stdout: 'pipe', stderr: 'pipe', env: { ...process.env, CERTUS_EMAIL_CHECK_ACCOUNT: email } });
    const output = await new Response(child.stdout).text();
    const errors = await new Response(child.stderr).text();
    assert.equal(await child.exited, 0, `Unavailable endpoint check failed: ${errors}`);
    assert.ok(output.includes('Unavailable endpoint proof passed.'));
  }

  const originalPort = process.env.SMTP_PORT;
  const originalMode = process.env.AUTH_MAIL_MODE;
  try {
    process.env.AUTH_MAIL_MODE = 'disabled';
    await assert.rejects(createAuthMail().assertAvailable(), { status: 'SERVICE_UNAVAILABLE' });
    process.env.AUTH_MAIL_MODE = 'smtp';
    process.env.SMTP_PORT = '1';
    await assert.rejects(createAuthMail().assertAvailable(), { status: 'SERVICE_UNAVAILABLE' });
    const sockets = new Set<import('bun').Socket>();
    const stalled = Bun.listen({ hostname: '127.0.0.1', port: 0, socket: {
      open(socket) { sockets.add(socket); }, data() {}, close(socket) { sockets.delete(socket); },
    } });
    try {
      process.env.SMTP_PORT = String(stalled.port);
      const start = Date.now();
      await assert.rejects(createAuthMail().assertAvailable(), { status: 'SERVICE_UNAVAILABLE' });
      assert.ok(Date.now() - start < 6000, 'SMTP greeting timeout stays bounded');
    } finally {
      for (const socket of sockets) socket.end();
      stalled.stop(true);
    }
  } finally {
    process.env.SMTP_PORT = originalPort;
    process.env.AUTH_MAIL_MODE = originalMode;
  }
  console.log('Auth email proof passed: real SMTP sink, signup, unverified rejection, resend, safe redirects, expiry, replay, reset, session revocation, password replacement, enumeration resistance, rate control, and unavailable transport.');
} finally {
  try {
    await removeMessages();
    if (!userId) {
      const found = await getDb().query('SELECT id FROM "user" WHERE email = $1', [email]);
      userId = found.rows[0]?.id;
    }
    if (userId) {
      await getDb().query('DELETE FROM verification WHERE value = $1', [userId]);
      await getDb().query('DELETE FROM "user" WHERE id = $1', [userId]);
      assert.equal((await getDb().query('SELECT id FROM "user" WHERE id = $1', [userId])).rowCount, 0);
    }
  } finally {
    await closeDbPool();
  }
}
