import { describe, expect, test } from 'bun:test';
import { apiKeyAuthorizesMethod } from '../../services/gateway/src/utils/apiKeyScopes';

describe('API key scope enforcement', () => {
  test('read keys cannot mutate resources', () => {
    expect(apiKeyAuthorizesMethod(['read'], 'GET')).toBe(true);
    expect(apiKeyAuthorizesMethod(['read'], 'POST')).toBe(false);
    expect(apiKeyAuthorizesMethod(['read'], 'DELETE')).toBe(false);
  });

  test('write includes reads and mutations while admin allows every method', () => {
    expect(apiKeyAuthorizesMethod(['write'], 'GET')).toBe(true);
    expect(apiKeyAuthorizesMethod(['write'], 'PATCH')).toBe(true);
    expect(apiKeyAuthorizesMethod(['admin'], 'DELETE')).toBe(true);
  });

  test('unknown or empty scopes fail closed', () => {
    expect(apiKeyAuthorizesMethod([], 'GET')).toBe(false);
    expect(apiKeyAuthorizesMethod(['unknown'], 'POST')).toBe(false);
  });
});
