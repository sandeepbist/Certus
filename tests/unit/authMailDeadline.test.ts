import { expect, test } from 'bun:test';
import net from 'node:net';
import { createAuthMail } from '../../services/web/src/lib/auth-mail';

for (const operation of ['preflight', 'send'] as const) {
  test(`auth email ${operation} cancels paced SMTP replies within ten seconds`, async () => {
    const original = { ...process.env };
    const peers = new Set<net.Socket>();
    const drips = new Set<ReturnType<typeof setInterval>>();
    let dripCount = 0;
    let closed = false;
    let settled = false;
    let unavailable = false;
    let messageReceived = false;
    let start = 0;
    let settledMs = Infinity;
    let closedMs = Infinity;
    const server = net.createServer(socket => {
      peers.add(socket);
      socket.setEncoding('utf8');
      socket.on('error', () => {});
      socket.on('close', () => { peers.delete(socket); closed = true; closedMs = performance.now() - start; });
      socket.write('220 deadline-fixture ESMTP\r\n');
      let buffer = '';
      let data = false;
      const pace = () => {
        socket.write('250-incomplete reply\r\n');
        const timer = setInterval(() => {
          dripCount++;
          socket.write('250-incomplete reply\r\n');
        }, 4000);
        drips.add(timer);
        socket.once('close', () => { clearInterval(timer); drips.delete(timer); });
      };
      socket.on('data', part => {
        buffer += part;
        while (buffer.includes('\n')) {
          const index = buffer.indexOf('\n');
          const line = buffer.slice(0, index).trim();
          buffer = buffer.slice(index + 1);
          if (data) {
            if (line === '.') { data = false; messageReceived = true; pace(); }
          } else if (line.startsWith('EHLO')) {
            if (operation === 'preflight') pace();
            else socket.write('250 deadline-fixture\r\n');
          } else if (line.startsWith('MAIL FROM:') || line.startsWith('RCPT TO:')) {
            socket.write('250 OK\r\n');
          } else if (line === 'DATA') {
            data = true;
            socket.write('354 Send message\r\n');
          }
        }
      });
    });
    let observation: ReturnType<typeof setTimeout> | undefined;
    let pending: Promise<unknown> | undefined;
    try {
      await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
      const address = server.address();
      if (!address || typeof address === 'string') throw new Error('Expected local SMTP port');
      Object.assign(process.env, {
        NODE_ENV: 'test', AUTH_MAIL_MODE: 'smtp', AUTH_MAIL_FROM: 'auth@certus.example',
        SMTP_HOST: '127.0.0.1', SMTP_PORT: String(address.port), SMTP_SECURITY: 'plain',
        SMTP_USER: '', SMTP_PASSWORD: '',
      });
      const mail = createAuthMail();
      start = performance.now();
      pending = (operation === 'preflight' ? mail.assertAvailable() : mail.send({
        email: 'fixture@example.invalid', url: 'http://localhost/private-fixture', kind: 'verification',
      })).catch(error => { unavailable = error.status === 'SERVICE_UNAVAILABLE'; })
        .finally(() => { settled = true; settledMs = performance.now() - start; });
      await new Promise<void>(resolve => { observation = setTimeout(resolve, 11_500); });
      // Observe before cleanup: destroying the peer must never make this check pass.
      expect({ settled, closed, sockets: peers.size }).toEqual({ settled: true, closed: true, sockets: 0 });
      expect(dripCount).toBeGreaterThanOrEqual(2);
      expect(unavailable).toBe(operation === 'preflight');
      expect(messageReceived).toBe(operation === 'send');
      expect(settledMs).toBeLessThan(11_000);
      expect(closedMs).toBeLessThan(11_000);
      console.info('SMTP deadline proof:', JSON.stringify({ operation, settledMs, closedMs, dripCount, sockets: peers.size }));
    } finally {
      clearTimeout(observation);
      for (const timer of drips) clearInterval(timer);
      for (const socket of peers) socket.destroy();
      await new Promise<void>(resolve => server.close(() => resolve()));
      await pending;
      process.env = original;
    }
  }, 15_000);
}
