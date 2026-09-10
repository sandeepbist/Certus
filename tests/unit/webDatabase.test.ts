import { describe, expect, test } from 'bun:test';

import {
  boundedDatabaseInteger,
  createDatabasePool,
  type WebDatabaseSettings,
} from '../../services/web/src/lib/db';

const testSettings: WebDatabaseSettings = {
  max: 2,
  idleTimeoutMillis: 1_234,
  connectionTimeoutMillis: 567,
  statementTimeoutMillis: 890,
  lockTimeoutMillis: 321,
  idleTransactionTimeoutMillis: 654,
  maxLifetimeSeconds: 30,
};

describe('Certus Web PostgreSQL pool', () => {
  test('rejects malformed and out-of-range resource settings', () => {
    expect(boundedDatabaseInteger('TEST_SETTING', undefined, 5, 1, 10)).toBe(5);
    expect(boundedDatabaseInteger('TEST_SETTING', '7', 5, 1, 10)).toBe(7);
    expect(() => boundedDatabaseInteger('TEST_SETTING', '7.5', 5, 1, 10)).toThrow();
    expect(() => boundedDatabaseInteger('TEST_SETTING', '0', 5, 1, 10)).toThrow();
    expect(() => boundedDatabaseInteger('TEST_SETTING', '11', 5, 1, 10)).toThrow();
  });

  test('applies every lifecycle bound and labels its connections', async () => {
    const pool = createDatabasePool(
      'postgresql://certus:certus@127.0.0.1:1/certus',
      testSettings,
    );
    try {
      expect(pool.options).toMatchObject({
        application_name: 'certus-web',
        max: 2,
        idleTimeoutMillis: 1_234,
        connectionTimeoutMillis: 567,
        statement_timeout: 890,
        lock_timeout: 321,
        idle_in_transaction_session_timeout: 654,
        maxLifetimeSeconds: 30,
        allowExitOnIdle: true,
      });
      expect(pool.listenerCount('error')).toBe(1);
    } finally {
      await pool.end();
    }
  });
});
