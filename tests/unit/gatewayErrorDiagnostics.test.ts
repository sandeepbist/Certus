import { describe, expect, test } from 'bun:test';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import {
  safeErrorFields,
  safeErrorType,
} from '../../services/gateway/src/utils/safeErrors';

function typescriptSources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return typescriptSources(entryPath);
    return entry.name.endsWith('.ts') ? [readFileSync(entryPath, 'utf8')] : [];
  });
}

describe('Gateway diagnostic boundary', () => {
  test('uses an exception class without reading mutable instance data', () => {
    class ProviderFailure extends Error {}

    const secret = 'Authorization: Bearer gateway-private-token';
    const poisoned = new Error(secret);
    poisoned.name = secret;

    expect(safeErrorType(poisoned)).toBe('Error');
    expect(safeErrorFields(new ProviderFailure(secret), 'provider/request')).toEqual({
      operation: 'provider_request',
      errorType: 'ProviderFailure',
    });
    expect(safeErrorType(secret)).toBe('ThrownString');
    expect(JSON.stringify(safeErrorFields(poisoned, 'gateway request'))).not.toContain(secret);
  });

  test('does not attach caught error objects to Gateway logs', () => {
    const source = typescriptSources(
      path.resolve(import.meta.dir, '../../services/gateway/src'),
    ).join('\n');

    expect(source).not.toMatch(/\{\s*err\s*:/);
    expect(source).not.toContain("console.error('Fatal error during Gateway startup:', err)");
  });
});
