import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:net';
import { enqueueAuthMail, deliverNextAuthMail, startAuthMailWorker, stopAuthMailWorker } from '../services/web/src/lib/auth-mail-outbox';
import { closeDbPool, getDb } from '../services/web/src/lib/db';

const databaseUrl = process.env.CERTUS_TEST_DATABASE_URL;
assert.ok(databaseUrl && ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(databaseUrl).hostname), 'Use a disposable local migrated database.');
Object.assign(process.env, { DATABASE_URL: databaseUrl, NODE_ENV: 'test',
  BETTER_AUTH_SECRET: 'certus-disposable-outbox-check-secret',
  BETTER_AUTH_URL: 'http://localhost:3000', APP_URL: 'http://localhost:3000', AUTH_MAIL_MODE: 'smtp',
  GOOGLE_CLIENT_ID: '', GOOGLE_CLIENT_SECRET: '', GITHUB_CLIENT_ID: '', GITHUB_CLIENT_SECRET: '',
  AUTH_MAIL_FROM: 'auth@certus.example', SMTP_HOST: '127.0.0.1', SMTP_SECURITY: 'plain', SMTP_USER: '', SMTP_PASSWORD: '',
});
const db = getDb();
const ids = new Set<string>();
const recipient = `outbox-${randomUUID()}@example.invalid`;
let attempts = 0;
let paced = false;
let drips = 0;
const timers = new Set<ReturnType<typeof setInterval>>();
const sockets = new Set<import('node:net').Socket>();
const server = createServer(socket => {
  sockets.add(socket); socket.on('error', () => {}); socket.on('close', () => sockets.delete(socket));
  socket.write('220 localhost ESMTP\r\n');
  let buffer = '';
  let data = false;
  socket.on('data', chunk => {
    buffer += chunk;
    while (buffer.includes('\r\n')) {
      const end = buffer.indexOf('\r\n'); const line = buffer.slice(0, end); buffer = buffer.slice(end + 2);
      if (data) {
        if (line === '.') {
          data = false; drips++; socket.write('250-wait\r\n');
          const timer = setInterval(() => { drips++; socket.write('250-wait\r\n'); }, 4000);
          timers.add(timer); socket.on('close', () => { clearInterval(timer); timers.delete(timer); });
        }
        continue;
      }
      if (line === 'DATA' && paced) { data = true; socket.write('354 send\r\n'); }
      else if (line.startsWith('EHLO')) socket.write('250 localhost\r\n');
      else if (line.startsWith('RCPT TO')) { attempts++; socket.write(paced ? '250 ok\r\n' : '550 rejected\r\n'); }
      else if (line === 'QUIT') socket.end('221 bye\r\n');
      else socket.write('250 ok\r\n');
    }
  });
});
await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
const address = server.address(); assert.ok(address && typeof address !== 'string');
process.env.SMTP_PORT = String(address.port);
async function queue() {
  const before = new Set((await db.query<{ id: string }>('SELECT id FROM auth_mail_outbox')).rows.map(row => row.id));
  await enqueueAuthMail([{ email: recipient, url: 'http://localhost:3000/api/auth/verify-email?token=disposable-private-token', kind: 'verification' }]);
  const rows = (await db.query<{ id: string; ciphertext: Buffer }>('SELECT id, ciphertext FROM auth_mail_outbox')).rows.filter(row => !before.has(row.id));
  assert.equal(rows.length, 1); const row = rows[0]; ids.add(row.id);
  assert(!row.ciphertext.includes(Buffer.from(recipient))); assert(!row.ciphertext.includes(Buffer.from('disposable-private-token')));
  return row.id;
}
async function state(id: string) { return (await db.query('SELECT status, ciphertext FROM auth_mail_outbox WHERE id=$1', [id])).rows[0]; }
try {
  assert.equal((await db.query('SELECT id FROM auth_mail_outbox')).rowCount, 0, 'Fixture queue must start empty');
  const rejected = await queue();
  await Promise.all([deliverNextAuthMail(), deliverNextAuthMail()]);
  assert.equal((await state(rejected)).status, 'uncertain'); assert.equal((await state(rejected)).ciphertext, null);
  await deliverNextAuthMail(); assert.equal(attempts, 1, 'Recipient rejection has no automatic retry');
  const unreadable = await queue();
  const secret = process.env.BETTER_AUTH_SECRET; process.env.BETTER_AUTH_SECRET += '-rotated';
  await deliverNextAuthMail(); process.env.BETTER_AUTH_SECRET = secret;
  assert.equal((await state(unreadable)).status, 'uncertain');
  assert.equal((await state(unreadable)).ciphertext, null); assert.equal(attempts, 1, 'Unreadable private payloads never send');
  process.env.SMTP_PORT = '1';
  const offline = await queue(); await deliverNextAuthMail(); assert.equal((await state(offline)).status, 'uncertain');
  const expired = await queue(); await db.query("UPDATE auth_mail_outbox SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1", [expired]);
  const interrupted = await queue(); await db.query("UPDATE auth_mail_outbox SET status='sending', started_at=NOW()-INTERVAL '61 seconds' WHERE id=$1", [interrupted]);
  await deliverNextAuthMail();
  assert.equal((await state(expired)).status, 'expired'); assert.equal((await state(expired)).ciphertext, null);
  assert.equal((await state(interrupted)).status, 'uncertain'); assert.equal((await state(interrupted)).ciphertext, null);
  assert.equal(attempts, 1, 'Expired and interrupted messages never retry');
  await db.query("UPDATE auth_mail_outbox SET finished_at=NOW()-INTERVAL '25 hours' WHERE id=ANY($1::uuid[])", [[...ids]]);
  await deliverNextAuthMail();
  for (const id of ids) assert.equal(await state(id), undefined, 'Terminal records expire');
  ids.clear();
  for (let i = 0; i < 1000; i++) ids.add(randomUUID());
  await db.query("INSERT INTO auth_mail_outbox(id,status,finished_at) SELECT unnest($1::uuid[]), 'uncertain', NOW()", [[...ids]]);
  const { getAuth } = await import('../services/web/src/lib/auth');
  process.env.SMTP_PORT = String(address.port);
  const responses = [];
  const userId = randomUUID();
  await db.query('INSERT INTO "user"(id,name,email,"emailVerified","createdAt","updatedAt") VALUES($1,\'Outbox fixture\',$2,false,NOW(),NOW())', [userId, recipient]);
  try {
    for (const path of ['/send-verification-email', '/request-password-reset']) {
      for (const email of [recipient, 'unknown-' + randomUUID() + '@example.invalid']) {
        const response = await getAuth().handler(new Request('http://localhost:3000/api/auth' + path, {
          method: 'POST', headers: { origin: 'http://localhost:3000', 'content-type': 'application/json', 'x-forwarded-for': '192.0.2.' + (responses.length + 1) },
          body: JSON.stringify({ email }),
        }));
        assert.equal(response.status, 503, 'Admission fails closed for known and unknown accounts');
        responses.push(await response.text());
      }
    }
    assert.equal(new Set(responses).size, 1, 'Admission failure does not enumerate accounts');
  } finally {
    await db.query('DELETE FROM verification WHERE value=$1', [userId]);
    await db.query('DELETE FROM "user" WHERE id=$1', [userId]);
  }
  await db.query('DELETE FROM auth_mail_outbox WHERE id=ANY($1::uuid[])', [[...ids]]); ids.clear();
  const timingUser = randomUUID();
  await db.query('INSERT INTO "user"(id,name,email,"emailVerified","createdAt","updatedAt") VALUES($1,\'Timing fixture\',$2,false,NOW(),NOW())', [timingUser, recipient]);
  paced = true;
  await startAuthMailWorker();
  try {
    let ip = 20;
    for (const path of ['/send-verification-email', '/request-password-reset']) {
      const observations = [];
      for (const email of [recipient, 'unknown-' + randomUUID() + '@example.invalid']) {
        const start = performance.now();
        const response = await getAuth().handler(new Request('http://localhost:3000/api/auth' + path, {
          method: 'POST', headers: { origin: 'http://localhost:3000', 'content-type': 'application/json', 'x-forwarded-for': '192.0.2.' + ip++ }, body: JSON.stringify({ email }),
        }));
        observations.push({ status: response.status, body: await response.text(), ms: performance.now() - start });
      }
      assert.equal(observations[0].status, 200); assert.equal(observations[1].status, 200);
      assert.equal(observations[0].body, observations[1].body);
      assert.ok(observations[0].ms < 2000 && observations[1].ms < 2000, 'HTTP settles before paced SMTP');
      assert.ok(Math.abs(observations[0].ms - observations[1].ms) < 1000, 'Recipient SMTP does not affect response timing');
    }
    assert.ok(drips > 0, 'Real recipient DATA remains in flight after public responses');
    await stopAuthMailWorker();
    assert.ok(drips >= 3, 'Tracked shutdown waits for the bounded SMTP attempt');
  } finally {
    await stopAuthMailWorker(); paced = false;
    while (await deliverNextAuthMail()) { /* Finish this disposable check's remaining jobs. */ }
    for (const row of (await db.query<{ id: string }>('SELECT id FROM auth_mail_outbox')).rows) ids.add(row.id);
    await db.query('DELETE FROM verification WHERE value=$1', [timingUser]);
    await db.query('DELETE FROM "user" WHERE id=$1', [timingUser]);
  }
  await db.query('DELETE FROM auth_mail_outbox WHERE id=ANY($1::uuid[])', [[...ids]]); ids.clear();
  await enqueueAuthMail([]);
  await startAuthMailWorker(); await stopAuthMailWorker();
  await startAuthMailWorker(); await stopAuthMailWorker();
  assert.equal((await db.query('SELECT id FROM auth_mail_outbox')).rowCount, 0, 'Tracked worker restart consumes durable no-op work');
  console.log('Auth outbox proof passed: encrypted payloads, bounded admission, generic failures, one attempt, offline/reject, expiry, interrupted recovery, retention, paced HTTP timing, and tracked worker restart.');
} finally {
  await stopAuthMailWorker();
  if (ids.size) await db.query('DELETE FROM auth_mail_outbox WHERE id=ANY($1::uuid[])', [[...ids]]);
  for (const timer of timers) clearInterval(timer);
  for (const socket of sockets) socket.destroy();
  await new Promise<void>(resolve => server.close(() => resolve()));
  await closeDbPool();
}
