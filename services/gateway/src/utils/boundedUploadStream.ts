import { Transform, type TransformCallback } from 'node:stream';

export const MAX_MULTIPART_ENVELOPE_BYTES = (50 * 1024 * 1024) + (64 * 1024);

export class UploadEnvelopeTooLargeError extends Error {}

export class BoundedUploadStream extends Transform {
  private byteLength = 0;

  constructor(private readonly maxBytes = MAX_MULTIPART_ENVELOPE_BYTES) {
    super();
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) {
      throw new Error('Upload stream bound must be a positive safe integer.');
    }
  }

  _transform(chunk: Buffer, _encoding: BufferEncoding, callback: TransformCallback) {
    this.byteLength += chunk.byteLength;
    if (this.byteLength > this.maxBytes) {
      callback(new UploadEnvelopeTooLargeError('Multipart upload envelope is too large.'));
      return;
    }
    callback(null, chunk);
  }
}

export function uploadEnvelopeIsTooLarge(error: unknown): boolean {
  let candidate: unknown = error;
  for (let depth = 0; depth < 4 && candidate; depth += 1) {
    if (candidate instanceof UploadEnvelopeTooLargeError) return true;
    candidate = typeof candidate === 'object' && 'cause' in candidate
      ? (candidate as { cause?: unknown }).cause
      : undefined;
  }
  return false;
}
