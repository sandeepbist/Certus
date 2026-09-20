import { assertWebDatabaseConfiguration, closeDbPool } from './lib/db';
import { safeErrorFields } from './lib/safe-errors';

const globalForShutdown = globalThis as typeof globalThis & {
  certusWebShutdownRegistered?: boolean;
};

assertWebDatabaseConfiguration();

if (!globalForShutdown.certusWebShutdownRegistered) {
  globalForShutdown.certusWebShutdownRegistered = true;
  const closeDatabase = () => {
    void closeDbPool().catch((error) => {
      console.error(
        'Could not close the Certus Web database pool.',
        safeErrorFields(error, 'Web database pool shutdown'),
      );
    });
  };
  process.once('SIGINT', closeDatabase);
  process.once('SIGTERM', closeDatabase);
}
