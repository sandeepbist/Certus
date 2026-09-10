import pg from 'pg';

const { Pool } = pg;
type DatabasePool = InstanceType<typeof Pool>;

export type WebDatabaseSettings = {
  max: number;
  idleTimeoutMillis: number;
  connectionTimeoutMillis: number;
  statementTimeoutMillis: number;
  lockTimeoutMillis: number;
  idleTransactionTimeoutMillis: number;
  maxLifetimeSeconds: number;
};

const globalForDatabase = globalThis as typeof globalThis & {
  certusDatabasePool?: DatabasePool;
  certusDatabasePoolClosing?: Promise<void>;
};

export function boundedDatabaseInteger(
  name: string,
  rawValue: string | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const value = rawValue === undefined || rawValue.trim() === ''
    ? fallback
    : Number(rawValue);
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer between ${minimum} and ${maximum}.`);
  }
  return value;
}

export function webDatabaseSettingsFromEnvironment(): WebDatabaseSettings {
  return {
    max: boundedDatabaseInteger(
      'WEB_DB_POOL_MAX_SIZE',
      process.env.WEB_DB_POOL_MAX_SIZE,
      process.env.NODE_ENV === 'production' ? 10 : 5,
      1,
      50,
    ),
    connectionTimeoutMillis: boundedDatabaseInteger(
      'WEB_DB_ACQUIRE_CONNECT_TIMEOUT_MS',
      process.env.WEB_DB_ACQUIRE_CONNECT_TIMEOUT_MS,
      3_000,
      100,
      30_000,
    ),
    idleTimeoutMillis: boundedDatabaseInteger(
      'WEB_DB_IDLE_TIMEOUT_MS',
      process.env.WEB_DB_IDLE_TIMEOUT_MS,
      30_000,
      1_000,
      600_000,
    ),
    statementTimeoutMillis: boundedDatabaseInteger(
      'WEB_DB_STATEMENT_TIMEOUT_MS',
      process.env.WEB_DB_STATEMENT_TIMEOUT_MS,
      10_000,
      100,
      300_000,
    ),
    lockTimeoutMillis: boundedDatabaseInteger(
      'WEB_DB_LOCK_TIMEOUT_MS',
      process.env.WEB_DB_LOCK_TIMEOUT_MS,
      3_000,
      100,
      60_000,
    ),
    idleTransactionTimeoutMillis: boundedDatabaseInteger(
      'WEB_DB_IDLE_TRANSACTION_TIMEOUT_MS',
      process.env.WEB_DB_IDLE_TRANSACTION_TIMEOUT_MS,
      10_000,
      100,
      300_000,
    ),
    maxLifetimeSeconds: boundedDatabaseInteger(
      'WEB_DB_MAX_LIFETIME_SECONDS',
      process.env.WEB_DB_MAX_LIFETIME_SECONDS,
      300,
      30,
      86_400,
    ),
  };
}

export function createDatabasePool(
  connectionString: string,
  settings: WebDatabaseSettings = webDatabaseSettingsFromEnvironment(),
): DatabasePool {
  const pool = new Pool({
    connectionString,
    application_name: 'certus-web',
    max: settings.max,
    idleTimeoutMillis: settings.idleTimeoutMillis,
    connectionTimeoutMillis: settings.connectionTimeoutMillis,
    statement_timeout: settings.statementTimeoutMillis,
    lock_timeout: settings.lockTimeoutMillis,
    idle_in_transaction_session_timeout: settings.idleTransactionTimeoutMillis,
    maxLifetimeSeconds: settings.maxLifetimeSeconds,
    allowExitOnIdle: true,
  });
  pool.on('error', (error) => {
    console.error('Unexpected error on an idle Certus Web database client.', error);
  });
  return pool;
}

export function assertWebDatabaseConfiguration(): void {
  const databaseUrl = process.env.DATABASE_URL?.trim();
  if (!databaseUrl && process.env.NODE_ENV === 'production') {
    throw new Error('DATABASE_URL must be configured in production.');
  }
  webDatabaseSettingsFromEnvironment();
}

export function getDb(): DatabasePool {
  if (globalForDatabase.certusDatabasePool) {
    return globalForDatabase.certusDatabasePool;
  }
  if (globalForDatabase.certusDatabasePoolClosing) {
    throw new Error('The Certus Web database pool is shutting down.');
  }

  const databaseUrl = process.env.DATABASE_URL?.trim();
  assertWebDatabaseConfiguration();
  const pool = createDatabasePool(
    databaseUrl || 'postgresql://nexus:nexus_dev_password@localhost:5432/nexus',
  );

  globalForDatabase.certusDatabasePool = pool;
  return pool;
}

export async function closeDbPool(): Promise<void> {
  if (globalForDatabase.certusDatabasePoolClosing) {
    return globalForDatabase.certusDatabasePoolClosing;
  }
  const pool = globalForDatabase.certusDatabasePool;
  if (!pool) return;

  const closing = pool.end().finally(() => {
    if (globalForDatabase.certusDatabasePool === pool) {
      globalForDatabase.certusDatabasePool = undefined;
    }
    if (globalForDatabase.certusDatabasePoolClosing === closing) {
      globalForDatabase.certusDatabasePoolClosing = undefined;
    }
  });
  globalForDatabase.certusDatabasePoolClosing = closing;
  return closing;
}
