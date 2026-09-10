import { describe, expect, test } from 'bun:test';

import { parseNotificationStreamEvent } from '../../services/gateway/src/websocket/notificationBroker';

function streamFields(event: Record<string, unknown>) {
  return ['event_id', String(event.event_id), 'event_json', JSON.stringify(event)];
}

describe('notification stream event validation', () => {
  test('accepts a tenant-routed notification envelope', () => {
    const event = {
      event_id: crypto.randomUUID(),
      notification_id: crypto.randomUUID(),
      tenant_id: 'tenant-one',
      user_id: 'user-one',
      event_type: 'created',
      notification: {
        id: crypto.randomUUID(),
        type: 'document_ready',
        title: 'Document ready',
        body: 'The document is indexed.',
        metadata: { document_id: crypto.randomUUID() },
        action_url: '/documents/one',
        is_read: false,
        created_at: new Date().toISOString(),
      },
      created_at: Math.floor(Date.now() / 1_000),
    };

    expect(parseNotificationStreamEvent(streamFields(event))).toEqual(event);
  });

  test('rejects malformed JSON and unrecognized event types', () => {
    expect(parseNotificationStreamEvent(['event_json', '{bad json'])).toBeNull();
    expect(parseNotificationStreamEvent(streamFields({
      event_id: crypto.randomUUID(),
      notification_id: crypto.randomUUID(),
      tenant_id: 'tenant-one',
      user_id: 'user-one',
      event_type: 'cross_tenant_broadcast',
      notification: {},
      created_at: 1,
    }))).toBeNull();
  });
});
