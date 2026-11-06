import { createCipheriv, createDecipheriv, hkdfSync, randomBytes, randomUUID } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { APIError } from 'better-auth/api';
import { createAuthMail, type AuthMailMessage } from './auth-mail';
import { getDb } from './db';
import { safeErrorFields } from './safe-errors';

type Job = { id: string; ciphertext: Buffer };
type Worker = { stop: AbortController; done: Promise<void> };
const runtime = globalThis as typeof globalThis & { certusAuthMailWorker?: Worker };

function key() {
  const secret = process.env.BETTER_AUTH_SECRET?.trim();
  if (!secret) throw new Error('BETTER_AUTH_SECRET is required for the auth mail outbox.');
  return Buffer.from(hkdfSync('sha256', secret, '', 'certus:auth-mail-outbox:v1', 32));
}

async function persistAuthMail(messages: AuthMailMessage[]) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key(), iv);
  const body = Buffer.concat([cipher.update(JSON.stringify(messages)), cipher.final()]);
  const ciphertext = Buffer.concat([iv, cipher.getAuthTag(), body]);
  const db = await getDb().connect();
  try {
    await db.query('BEGIN');
    // Serialize bounded admission across replicas, including unknown recipients.
    await db.query("SELECT pg_advisory_xact_lock(hashtextextended('certus:auth-mail-admission:v1', 0))");
    const result = await db.query(`INSERT INTO auth_mail_outbox (id, ciphertext)
      SELECT $1, $2 WHERE (SELECT count(*) FROM auth_mail_outbox) < 1000 RETURNING id`, [randomUUID(), ciphertext]);
    if (result.rowCount !== 1) throw new Error('Auth mail outbox capacity reached.');
    await db.query('COMMIT');
  } catch (error) {
    await db.query('ROLLBACK');
    throw error;
  } finally { db.release(); }
}

export async function enqueueAuthMail(messages: AuthMailMessage[]) {
  try {
    if (messages.length > 2) throw new Error('Auth mail request exceeds payload limit.');
    await persistAuthMail(messages);
  } catch (error) {
    console.error('Certus auth email admission failed.', safeErrorFields(error, 'auth email admission'));
    throw new APIError('SERVICE_UNAVAILABLE', {
      code: 'AUTH_MAIL_UNAVAILABLE', message: 'Authentication email is unavailable. Please try later or contact the operator.',
    });
  }
}

function decrypt(ciphertext: Buffer): AuthMailMessage[] {
  const decipher = createDecipheriv('aes-256-gcm', key(), ciphertext.subarray(0, 12));
  decipher.setAuthTag(ciphertext.subarray(12, 28));
  const value: unknown = JSON.parse(Buffer.concat([decipher.update(ciphertext.subarray(28)), decipher.final()]).toString());
  if (!Array.isArray(value) || value.length > 2) throw new Error('Invalid auth mail payload.');
  return value.map((message: unknown) => {
    if (!message || typeof message !== 'object' || !('email' in message) || typeof message.email !== 'string'
        || !('url' in message) || typeof message.url !== 'string' || !('kind' in message)
        || (message.kind !== 'verification' && message.kind !== 'reset')) throw new Error('Invalid auth mail payload.');
    return { email: message.email, url: message.url, kind: message.kind };
  });
}

export async function deliverNextAuthMail() {
  const db = getDb();
  await db.query(`DELETE FROM auth_mail_outbox WHERE finished_at < NOW() - INTERVAL '24 hours'`);
  const recovery = await db.query(`UPDATE auth_mail_outbox SET status = CASE WHEN status = 'sending' THEN 'uncertain' ELSE 'expired' END,
    ciphertext = NULL, finished_at = NOW() WHERE
    (status = 'sending' AND started_at < NOW() - INTERVAL '60 seconds') OR (status = 'pending' AND expires_at <= NOW())`);
  if (recovery.rowCount) console.error('Certus auth email expired or interrupted.', safeErrorFields(undefined, 'auth email recovery'));
  const next = await db.query<Job>(`UPDATE auth_mail_outbox SET status = 'sending', started_at = NOW()
    WHERE id = (SELECT id FROM auth_mail_outbox WHERE status = 'pending' AND expires_at > NOW()
      ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id, ciphertext`);
  const job = next.rows[0];
  if (!job) return false;
  try {
    const messages = decrypt(job.ciphertext);
    const mail = createAuthMail();
    for (const message of messages) {
      if (!await mail.send(message)) throw new Error('Auth mail attempt did not confirm acceptance.');
    }
    await db.query("DELETE FROM auth_mail_outbox WHERE id = $1 AND status = 'sending'", [job.id]);
  } catch (error) {
    // A timeout or crash can follow remote acceptance. Never retry an ambiguous SMTP attempt.
    await db.query("UPDATE auth_mail_outbox SET status = 'uncertain', ciphertext = NULL, finished_at = NOW() WHERE id = $1", [job.id]);
    console.error('Certus auth email attempt unconfirmed.', safeErrorFields(error, 'auth email outbox delivery'));
  }
  return true;
}

export async function startAuthMailWorker() {
  if (runtime.certusAuthMailWorker) return;
  if (createAuthMail().mode !== 'smtp') return;
  key();
  await getDb().query('SELECT id FROM auth_mail_outbox LIMIT 0');
  const stop = new AbortController();
  const done = (async () => {
    while (!stop.signal.aborted) {
      try {
        if (await deliverNextAuthMail()) continue;
      } catch (error) {
        console.error('Certus auth email worker unavailable.', safeErrorFields(error, 'auth email worker'));
      }
      try { await pause(1000, undefined, { signal: stop.signal }); } catch { break; }
    }
  })();
  runtime.certusAuthMailWorker = { stop, done };
}

export async function stopAuthMailWorker() {
  const worker = runtime.certusAuthMailWorker;
  if (!worker) return;
  worker.stop.abort();
  await worker.done;
  if (runtime.certusAuthMailWorker === worker) runtime.certusAuthMailWorker = undefined;
}
