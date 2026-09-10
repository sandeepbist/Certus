export type ApiKeyScope = 'read' | 'write' | 'admin';

const readMethods = new Set(['GET', 'HEAD', 'OPTIONS']);

export function apiKeyAuthorizesMethod(scopes: string[], method: string) {
  if (scopes.includes('admin')) return true;
  if (readMethods.has(method.toUpperCase())) {
    return scopes.includes('read') || scopes.includes('write');
  }
  return scopes.includes('write');
}
