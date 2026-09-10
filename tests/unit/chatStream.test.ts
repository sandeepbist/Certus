import { describe, expect, test } from 'bun:test';

import { parseSseFrame } from '../../services/gateway/src/websocket/streamHandler';

describe('chat SSE framing', () => {
  test('joins multiline data and accepts CRLF frames', () => {
    expect(parseSseFrame('event: update\r\ndata: {"type":"token",\r\ndata: "content":"hello"}\r\n'))
      .toBe('{"type":"token",\n"content":"hello"}');
  });

  test('ignores comments and frames without data', () => {
    expect(parseSseFrame(': keepalive\nretry: 1000')).toBeNull();
    expect(parseSseFrame('data: {"type":"done"}')).toBe('{"type":"done"}');
  });
});
