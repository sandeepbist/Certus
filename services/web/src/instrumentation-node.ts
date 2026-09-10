import { assertWebDatabaseConfiguration, closeDbPool } from './lib/db';

const globalForShutdown = globalThis as typeof globalThis & {
  certusWebShutdownRegistered?: boolean;
};

assertWebDatabaseConfiguration();

if (!globalForShutdown.certusWebShutdownRegistered) {
  globalForShutdown.certusWebShutdownRegistered = true;
  const closeDatabase = () => {
    void closeDbPool().catch((error) => {
      console.error('Could not close the Certus Web database pool.', error);
    });
  };
  process.once('SIGINT', closeDatabase);
  process.once('SIGTERM', closeDatabase);
}
