import type { Citation, ClaimAlignedSpan } from '../hooks/useStreamingChat';

export function documentEvidenceHref(citation: Citation, span?: ClaimAlignedSpan): string {
  const query = new URLSearchParams({ chunk: citation.chunkId });
  if (citation.versionNumber) query.set('version', String(citation.versionNumber));
  if (span?.textLocatorStatus === 'exact') {
    const { startChar, endChar } = span;
    if (
      typeof startChar !== 'number'
      || typeof endChar !== 'number'
      || !Number.isSafeInteger(startChar)
      || !Number.isSafeInteger(endChar)
      || startChar < 0
      || endChar <= startChar
      || !/^[a-f0-9]{64}$/.test(span.quoteSha256)
    ) {
      throw new Error('The citation source selection is invalid.');
    }
    query.set('start', String(startChar));
    query.set('end', String(endChar));
    query.set('quote_sha256', span.quoteSha256);
  }
  return `/documents/${encodeURIComponent(citation.documentId)}?${query}`;
}
