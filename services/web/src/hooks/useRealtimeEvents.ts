'use client';

import { useEffect, useRef } from 'react';

import { gatewayWebSocketUrl } from '@/lib/gateway-client';

export type RealtimeProductEvent = {
  type: 'trace.event' | 'trace.status' | 'document.status';
  event_id: string;
  channel: string;
  data: Record<string, unknown>;
};

function parseRealtimeEvent(value: unknown): RealtimeProductEvent | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const event = value as Partial<RealtimeProductEvent>;
  if (
    !['trace.event', 'trace.status', 'document.status'].includes(event.type || '')
    || typeof event.event_id !== 'string'
    || typeof event.channel !== 'string'
    || typeof event.data !== 'object'
    || event.data === null
    || Array.isArray(event.data)
  ) return null;
  return event as RealtimeProductEvent;
}

export function useRealtimeEvents(
  channels: string[],
  onEvent: (event: RealtimeProductEvent) => void,
  onSynchronize: () => void,
) {
  const eventHandlerRef = useRef(onEvent);
  const synchronizeRef = useRef(onSynchronize);
  eventHandlerRef.current = onEvent;
  synchronizeRef.current = onSynchronize;

  const channelKey = [...new Set(channels)].sort().join('\u0000');

  useEffect(() => {
    const subscribedChannels = channelKey ? channelKey.split('\u0000') : [];
    if (subscribedChannels.length === 0) return;

    let socket: WebSocket | null = null;
    let stopped = false;
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;

    const scheduleReconnect = () => {
      if (stopped || !navigator.onLine || reconnectTimer !== null) return;
      const baseDelay = Math.min(30_000, 1_000 * (2 ** reconnectAttempt));
      const delay = Math.round(baseDelay * (0.8 + Math.random() * 0.4));
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (
        stopped
        || !navigator.onLine
        || socket?.readyState === WebSocket.OPEN
        || socket?.readyState === WebSocket.CONNECTING
      ) return;
      try {
        const nextSocket = new WebSocket(gatewayWebSocketUrl('/realtime'));
        socket = nextSocket;
        nextSocket.onopen = () => {
          reconnectAttempt = 0;
          nextSocket.send(JSON.stringify({
            type: 'subscribe',
            channels: subscribedChannels,
          }));
        };
        nextSocket.onmessage = (message) => {
          try {
            const parsed: unknown = JSON.parse(String(message.data));
            if (
              typeof parsed === 'object'
              && parsed !== null
              && 'type' in parsed
              && parsed.type === 'subscribed'
            ) {
              synchronizeRef.current();
              return;
            }
            const event = parseRealtimeEvent(parsed);
            if (event && subscribedChannels.includes(event.channel)) {
              eventHandlerRef.current(event);
            }
          } catch {
            // Ignore malformed server messages; REST snapshots remain authoritative.
          }
        };
        nextSocket.onerror = () => nextSocket.close();
        nextSocket.onclose = () => {
          if (socket === nextSocket) socket = null;
          scheduleReconnect();
        };
      } catch {
        socket = null;
        scheduleReconnect();
      }
    };

    const onOnline = () => connect();
    const onFocus = () => {
      synchronizeRef.current();
      connect();
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        synchronizeRef.current();
        connect();
      }
    };
    const onOffline = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      socket?.close();
      socket = null;
    };
    const fallbackInterval = window.setInterval(() => {
      if (document.visibilityState === 'visible' && socket?.readyState !== WebSocket.OPEN) {
        synchronizeRef.current();
      }
    }, 60_000);

    connect();
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      window.clearInterval(fallbackInterval);
      socket?.close();
      socket = null;
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [channelKey]);
}
