import { describe, expect, test } from 'bun:test';

import {
  parseChatSessionId,
  parseSelectedDocumentIds,
  parseSelectedVersionScope,
} from '../../services/gateway/src/routes/chat';

describe('chat selected-document scope', () => {
  test('defaults to an unscoped request and canonicalizes duplicate UUIDs', () => {
    const documentId = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA';

    expect(parseSelectedDocumentIds(undefined)).toEqual([]);
    expect(parseSelectedDocumentIds([documentId, documentId.toLowerCase()]))
      .toEqual([documentId.toLowerCase()]);
  });

  test('rejects invalid and over-wide scopes', () => {
    expect(parseSelectedDocumentIds(['not-a-uuid'])).toBeNull();
    expect(parseSelectedDocumentIds(Array.from(
      { length: 11 },
      (_, index) => `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
    ))).toBeNull();
  });

  test('validates an explicit document-version scope', () => {
    expect(parseSelectedVersionScope(undefined)).toBe('auto');
    expect(parseSelectedVersionScope('auto')).toBe('auto');
    expect(parseSelectedVersionScope('all_history')).toBe('all_history');
    expect(parseSelectedVersionScope('current_only')).toBe('current_only');
    expect(parseSelectedVersionScope('current')).toBeNull();
    expect(parseSelectedVersionScope(null)).toBeNull();
  });

  test('requires a canonical session UUID and uses a stable request fallback', () => {
    const requestId = '00000000-0000-4000-8000-000000000001';
    const sessionId = 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA';

    expect(parseChatSessionId(sessionId, requestId)).toBe(sessionId.toLowerCase());
    expect(parseChatSessionId(undefined, requestId)).toBe(requestId);
    expect(parseChatSessionId('session_default', requestId)).toBeNull();
    expect(parseChatSessionId(null, requestId)).toBeNull();
  });
});
