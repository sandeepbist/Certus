import { describe, expect, test } from 'bun:test';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import {
  safeErrorFields,
  safeErrorType,
} from '../../services/web/src/lib/safe-errors';

function sourceFiles(directory: string): Array<{ path: string; source: string }> {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(entryPath);
    if (!entry.name.endsWith('.ts') && !entry.name.endsWith('.tsx')) return [];
    return [{ path: entryPath, source: readFileSync(entryPath, 'utf8') }];
  });
}

describe('Web diagnostic boundary', () => {
  test('uses an exception class without reading mutable instance data', () => {
    class DatabaseFailure extends Error {}

    const secret = 'postgresql://user:private-password@database.internal/certus';
    const poisoned = new Error(secret);
    poisoned.name = secret;

    expect(safeErrorType(poisoned)).toBe('Error');
    expect(safeErrorFields(new DatabaseFailure(secret), 'database/request')).toEqual({
      operation: 'database_request',
      errorType: 'DatabaseFailure',
    });
    expect(safeErrorType(secret)).toBe('ThrownString');
    expect(JSON.stringify(safeErrorFields(poisoned, 'Web request'))).not.toContain(secret);
  });

  test('sanitizes every Web console diagnostic and the global error response', () => {
    const webRoot = path.resolve(import.meta.dir, '../../services/web/src');
    const files = sourceFiles(webRoot);

    for (const file of files) {
      const consoleCalls = file.source.match(/console\.error\([\s\S]*?\);/g) || [];
      for (const consoleCall of consoleCalls) {
        expect(consoleCall, file.path).toContain('safeErrorFields(');
      }
    }

    const globalErrorSource = readFileSync(
      path.join(webRoot, 'app/error.tsx'),
      'utf8',
    );
    expect(globalErrorSource).not.toContain('{error.message');
    expect(globalErrorSource).toContain('An unexpected error occurred during execution.');
  });
});
