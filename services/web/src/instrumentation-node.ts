import { startAuthMailWorker, stopAuthMailWorker } from './lib/auth-mail-outbox';
import { createAuthMail } from './lib/auth-mail';
import { assertWebDatabaseConfiguration, closeDbPool } from './lib/db';
import { safeErrorFields } from './lib/safe-errors';

const globalForShutdown = globalThis as typeof globalThis & {
  certusWebShutdownRegistered?: boolean;
  certusWebShutdown?: Promise<void>;
};

assertWebDatabaseConfiguration();
createAuthMail();

export async function startAuthMailDelivery() {
  await startAuthMailWorker();
}

if (!globalForShutdown.certusWebShutdownRegistered) {
  globalForShutdown.certusWebShutdownRegistered = true;
  const closeDatabase = () => {
    globalForShutdown.certusWebShutdown = stopAuthMailWorker().then(() => closeDbPool()).catch((error) => {
      console.error(
        'Could not close the Certus Web database pool.',
        safeErrorFields(error, 'Web database pool shutdown'),
      );
    });
  };
  process.once('SIGINT', closeDatabase);
  process.once('SIGTERM', closeDatabase);
}
