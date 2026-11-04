import nodemailer from 'nodemailer';
import { APIError } from 'better-auth/api';
import { safeErrorFields } from './safe-errors';

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
  const transport = nodemailer.createTransport({
    host, port, secure: security === 'tls', requireTLS: security === 'starttls',
    ignoreTLS: security === 'plain',
    ...(user && pass ? { auth: { user, pass } } : {}),
    connectionTimeout: 3000, greetingTimeout: 3000, socketTimeout: 5000,
    dnsTimeout: 3000, logger: false, debug: false,
    disableFileAccess: true, disableUrlAccess: true,
  });
  return {
    mode,
    async assertAvailable() {
      try { await transport.verify(); } catch { throw unavailable(); }
    },
    async send({ email, url, kind }: { email: string; url: string; kind: 'verification' | 'reset' }) {
      const verification = kind === 'verification';
      try {
        const result = await transport.sendMail({
          from, to: email,
          subject: verification ? 'Verify your Certus email' : 'Reset your Certus password',
          text: `${verification ? 'Verify your email' : 'Reset your password'} using this private link:\n\n${url}\n\nThis link expires in ${verification ? '24 hours' : '1 hour'}. If you did not request it, ignore this email.`,
        });
        if (!result.accepted.includes(email)) throw unavailable();
      } catch (error) {
        // Better Auth exposes callback failures only for existing accounts.
        console.error('Certus auth email delivery failed.', safeErrorFields(error, 'auth email delivery'));
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
