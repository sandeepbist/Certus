import { describe, expect, test } from 'bun:test';

import { parseRealtimeStreamEvent } from '../../services/gateway/src/websocket/realtimeBroker';

function streamFields(event: Record<string, unknown>) {
  return ['event_id', String(event.event_id), 'event_json', JSON.stringify(event)];
}

describe('realtime product event validation', () => {
  test('accepts a user-routed trace event', () => {
    const runId = crypto.randomUUID();
    const event = {
      event_id: crypto.randomUUID(),
      tenant_id: 'tenant-one',
      user_id: 'user-one',
      channel: `trace:${runId}`,
      event_type: 'trace.event',
      data: {
        run_id: runId,
        agent: 'researcher',
        action: 'Retrieved workspace context',
        status: 'completed',
        token_count: 0,
      },
      created_at: Math.floor(Date.now() / 1_000),
    };

    expect(parseRealtimeStreamEvent(streamFields(event))).toEqual(event);
  });

  test('rejects unsupported channels and event types', () => {
    const base = {
      event_id: crypto.randomUUID(),
      tenant_id: 'tenant-one',
      user_id: 'user-one',
      data: {},
      created_at: 1,
    };
    expect(parseRealtimeStreamEvent(streamFields({
      ...base,
      channel: `notifications:${crypto.randomUUID()}`,
      event_type: 'document.status',
    }))).toBeNull();
    expect(parseRealtimeStreamEvent(streamFields({
      ...base,
      channel: `document:${crypto.randomUUID()}`,
      event_type: 'tenant.broadcast',
    }))).toBeNull();
  });
});
