import { afterEach, describe, expect, test } from 'bun:test';
import { createAuthMail } from '../../services/web/src/lib/auth-mail';

const original = { ...process.env };
afterEach(() => { process.env = { ...original }; });

function smtp() {
  Object.assign(process.env, {
    NODE_ENV: 'test', AUTH_MAIL_MODE: 'smtp', SMTP_HOST: '127.0.0.1',
    SMTP_PORT: '1025', SMTP_SECURITY: 'plain', AUTH_MAIL_FROM: 'auth@certus.example',
    SMTP_USER: '', SMTP_PASSWORD: '',
  });
}

describe('auth email configuration', () => {
  test('no-mail development fails honestly without changing verification policy', async () => {
    smtp();
    process.env.AUTH_MAIL_MODE = 'disabled';
    expect(createAuthMail().mode).toBe('disabled');
    await expect(createAuthMail().assertAvailable()).rejects.toMatchObject({ status: 'SERVICE_UNAVAILABLE' });
  });
  test('production rejects disabled, incomplete, plaintext, and unpaired credentials', () => {
    smtp();
    process.env.NODE_ENV = 'production';
    expect(() => createAuthMail()).toThrow('Invalid auth SMTP configuration');
    process.env.SMTP_SECURITY = 'starttls';
    expect(createAuthMail().mode).toBe('smtp');
    process.env.SMTP_HOST = '';
    expect(() => createAuthMail()).toThrow('Invalid auth SMTP configuration');
    process.env.SMTP_HOST = 'smtp.example.invalid';
    process.env.SMTP_USER = 'sender';
    expect(() => createAuthMail()).toThrow('Invalid auth SMTP configuration');
    process.env.AUTH_MAIL_MODE = 'disabled';
    expect(() => createAuthMail()).toThrow('disabled is allowed only outside production');
  });
  test('invalid ports, modes, sender injection, and remote plaintext are rejected', () => {
    for (const [key, value] of [
      ['SMTP_PORT', '0'], ['SMTP_PORT', 'NaN'], ['SMTP_PORT', '65536'],
      ['SMTP_SECURITY', 'invalid'], ['AUTH_MAIL_FROM', 'sender@example.invalid\r\nBcc:x@example.invalid'],
      ['SMTP_HOST', 'remote.example.invalid'], ['AUTH_MAIL_MODE', 'invalid'],
    ]) {
      smtp();
      process.env[key] = value;
      expect(() => createAuthMail()).toThrow();
    }
  });
});
