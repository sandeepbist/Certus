import nodemailer, { type Transporter } from 'nodemailer';
import type SMTPTransport from 'nodemailer/lib/smtp-transport';
import { connect } from 'node:net';
import { APIError } from 'better-auth/api';
import { safeErrorFields } from './safe-errors';

export type AuthMailMessage = { email: string; url: string; kind: 'verification' | 'reset' };

export function createAuthMail() {
  const production = process.env.NODE_ENV === 'production';
  const mode = process.env.AUTH_MAIL_MODE?.trim() || (production ? 'smtp' : 'disabled');
  if (mode === 'disabled' && !production) {
    return {
      mode,
      assertAvailable: async () => { throw unavailable(); },
      send: async () => { throw unavailable(); },
    };
  }
  if (mode !== 'smtp') throw new Error('AUTH_MAIL_MODE must be smtp; disabled is allowed only outside production.');

  const host = process.env.SMTP_HOST?.trim();
  const from = process.env.AUTH_MAIL_FROM?.trim();
  const port = Number(process.env.SMTP_PORT || '587');
  const security = process.env.SMTP_SECURITY || 'starttls';
  const user = process.env.SMTP_USER;
  const pass = process.env.SMTP_PASSWORD;
  if (!host || /[\s/@?#]/.test(host) || !from || !/^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$/.test(from)
      || !Number.isInteger(port) || port < 1 || port > 65535
      || !['tls', 'starttls', 'plain'].includes(security)
      || (security === 'plain' && (production || !['localhost', '127.0.0.1', '::1'].includes(host)))
      || Boolean(user) !== Boolean(pass)) {
    throw new Error('Invalid auth SMTP configuration. Configure host, port, sender, security, and paired credentials.');
  }
  const options = {
    host, port, secure: security === 'tls', requireTLS: security === 'starttls',
    ignoreTLS: security === 'plain',
    ...(user && pass ? { auth: { user, pass } } : {}),
    connectionTimeout: 3000, greetingTimeout: 3000, socketTimeout: 5000,
    dnsTimeout: 3000, logger: false, debug: false,
    disableFileAccess: true, disableUrlAccess: true,
  };
  async function withTransport<T>(operation: (transport: Transporter<SMTPTransport.SentMessageInfo>) => Promise<T>) {
    const controller = new AbortController();
    let connectionTimer: ReturnType<typeof setTimeout> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const transport = nodemailer.createTransport({
      ...options,
      getSocket(_options, callback) {
        if (controller.signal.aborted) return callback(unavailable(), false);
        // Own the raw socket; Nodemailer still handles TLS, STARTTLS and authentication.
        const connection = connect({ host, port, signal: controller.signal });
        const onError = (error: Error) => callback(error, false);
        connection.once('error', onError);
        connectionTimer = setTimeout(() => connection.destroy(new Error('SMTP connection timeout')), 3000);
        connection.once('connect', () => {
          clearTimeout(connectionTimer);
          connection.removeListener('error', onError);
          callback(null, { connection });
        });
      },
    } satisfies SMTPTransport.Options);
    try {
      const timeout = new Promise<never>((_resolve, reject) => {
        deadline = setTimeout(() => {
          controller.abort();
          reject(unavailable());
        }, 10_000);
      });
      return await Promise.race([operation(transport), timeout]);
    } finally {
      clearTimeout(deadline);
      clearTimeout(connectionTimer);
      controller.abort();
      transport.close();
    }
  }
  return {
    mode,
    async assertAvailable() {
      try { await withTransport(transport => transport.verify()); } catch { throw unavailable(); }
    },
    async send({ email, url, kind }: AuthMailMessage) {
      const verification = kind === 'verification';
      try {
        const result = await withTransport(transport => transport.sendMail({
          from, to: email,
          subject: verification ? 'Verify your Certus email' : 'Reset your Certus password',
          text: `${verification ? 'Verify your email' : 'Reset your password'} using this private link:\n\n${url}\n\nThis link expires ${verification ? '24 hours' : '1 hour'} after it was requested. If you did not request it, ignore this email.`,
        }));
        if (!result.accepted.includes(email)) throw unavailable();
        return true;
      } catch (error) {
        // Delivery outcomes remain private; public endpoints only confirm a durable request.
        console.error('Certus auth email delivery failed.', safeErrorFields(error, 'auth email delivery'));
        return false;
      }
    },
  };
}

function unavailable() {
  return new APIError('SERVICE_UNAVAILABLE', {
    code: 'AUTH_MAIL_UNAVAILABLE',
    message: 'Authentication email is unavailable. Please try later or contact the operator.',
  });
}
