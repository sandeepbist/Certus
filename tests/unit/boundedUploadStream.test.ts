import { describe, expect, test } from 'bun:test';
import { once } from 'node:events';

import {
  BoundedUploadStream,
  UploadEnvelopeTooLargeError,
  uploadEnvelopeIsTooLarge,
} from '../../services/gateway/src/utils/boundedUploadStream';

describe('bounded upload forwarding', () => {
  test('forwards chunks without coalescing the whole request', async () => {
    const stream = new BoundedUploadStream(10);
    const received: Buffer[] = [];
    stream.on('data', (chunk) => received.push(chunk));

    stream.write(Buffer.from('certus'));
    stream.end(Buffer.from('123'));
    await once(stream, 'end');

    expect(Buffer.concat(received).toString()).toBe('certus123');
    expect(received.length).toBe(2);
  });

  test('fails a chunked request as soon as its envelope crosses the bound', async () => {
    const stream = new BoundedUploadStream(5);
    stream.resume();
    stream.write(Buffer.from('1234'));
    stream.end(Buffer.from('56'));

    const [error] = await once(stream, 'error');
    expect(error).toBeInstanceOf(UploadEnvelopeTooLargeError);
    expect(uploadEnvelopeIsTooLarge({ cause: error })).toBe(true);
    expect(uploadEnvelopeIsTooLarge(new Error('unrelated'))).toBe(false);
  });
});
