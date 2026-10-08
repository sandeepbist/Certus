import { describe, expect, test } from 'bun:test';
import { documentEvidenceHref } from '../../services/web/src/lib/evidence-link';
import type { Citation, ClaimAlignedSpan } from '../../services/web/src/hooks/useStreamingChat';

const citation: Citation = {
  documentId: 'document', chunkId: 'chunk', documentTitle: 'Proof', quote: 'Full chunk', versionNumber: 2,
};
const span: ClaimAlignedSpan = {
  claimId: 'C1', profile: 'certus_claim_aligned_sentence_span:unicode_code_point:v1',
  selectionStatus: 'claim_aligned', quote: 'café', quoteSha256: 'a'.repeat(64),
  relativeStartChar: 2, relativeEndChar: 7, startChar: 4, endChar: 9,
  textLocatorStatus: 'exact', semanticEntailmentChecked: false,
};

describe('citation source links', () => {
  test('selects absolute artifact offsets and digest for the retained version', () => {
    const link = new URL(documentEvidenceHref(citation, span), 'http://localhost');
    expect(link.pathname).toBe('/documents/document');
    expect(Object.fromEntries(link.searchParams)).toEqual({
      chunk: 'chunk', version: '2', start: '4', end: '9', quote_sha256: 'a'.repeat(64),
    });
  });

  test('keeps full-chunk and legacy inspection explicit, rejects malformed exact spans', () => {
    expect(documentEvidenceHref(citation)).not.toContain('start=');
    expect(documentEvidenceHref(citation, { ...span, textLocatorStatus: 'unavailable' })).not.toContain('start=');
    expect(() => documentEvidenceHref(citation, { ...span, endChar: 4 })).toThrow();
    expect(() => documentEvidenceHref(citation, { ...span, quoteSha256: 'invalid' })).toThrow();
  });
});
