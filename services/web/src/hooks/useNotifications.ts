'use client';

import { useCallback, useEffect, useState } from 'react';

import { gatewayFetch, gatewayWebSocketUrl } from '@/lib/gateway-client';

export type NotificationItem = {
  id: string;
  type: string;
  title: string;
  body: string | null;
  metadata: Record<string, unknown>;
  action_url: string | null;
  is_read: boolean;
  created_at: string;
};

type NotificationSummary = {
  notifications: NotificationItem[];
  unread_count: number;
};

export const NOTIFICATIONS_CHANGED_EVENT = 'certus:notifications-changed';

type NotificationChangeSource = 'local' | 'realtime';

export function announceNotificationsChanged(source: NotificationChangeSource = 'local') {
  window.dispatchEvent(new CustomEvent(NOTIFICATIONS_CHANGED_EVENT, { detail: { source } }));
}

function isNotificationItem(value: unknown): value is NotificationItem {
  if (typeof value !== 'object' || value === null) return false;
  const item = value as Partial<NotificationItem>;
  return (
    typeof item.id === 'string'
    && typeof item.type === 'string'
    && typeof item.title === 'string'
    && (typeof item.body === 'string' || item.body === null)
    && typeof item.metadata === 'object'
    && item.metadata !== null
    && (typeof item.action_url === 'string' || item.action_url === null)
    && typeof item.is_read === 'boolean'
    && typeof item.created_at === 'string'
  );
}

function parseNotificationSync(value: unknown): NotificationSummary | null {
  if (typeof value !== 'object' || value === null) return null;
  const candidate = value as Partial<NotificationSummary>;
  if (
    !Array.isArray(candidate.notifications)
    || !candidate.notifications.every(isNotificationItem)
    || typeof candidate.unread_count !== 'number'
    || !Number.isInteger(candidate.unread_count)
    || candidate.unread_count < 0
  ) return null;
  return {
    notifications: candidate.notifications,
    unread_count: candidate.unread_count,
  };
}

export function useNotificationSummary(limit = 5) {
  const [summary, setSummary] = useState<NotificationSummary>({
    notifications: [],
    unread_count: 0,
  });
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await gatewayFetch(`/notifications?status=unread&limit=${limit}`, { signal });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.message || payload?.detail || 'Notifications could not be loaded.');
      }
      setSummary({
        notifications: payload.notifications || [],
        unread_count: payload.unread_count || 0,
      });
      setError(null);
    } catch (loadError) {
      if (loadError instanceof DOMException && loadError.name === 'AbortError') return;
      setError(loadError instanceof Error ? loadError.message : 'Notifications could not be loaded.');
    } finally {
      setIsLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    const controller = new AbortController();
    let socket: WebSocket | null = null;
    let stopped = false;
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;
    let realtimeRefreshTimer: number | null = null;
    void refresh(controller.signal);

    const onChanged = () => void refresh();
    const scheduleRealtimeRefresh = () => {
      if (realtimeRefreshTimer !== null) return;
      realtimeRefreshTimer = window.setTimeout(() => {
        realtimeRefreshTimer = null;
        announceNotificationsChanged('realtime');
      }, 75);
    };
    const scheduleReconnect = () => {
      if (stopped || !navigator.onLine || reconnectTimer !== null) return;
      const baseDelay = Math.min(30_000, 1_000 * (2 ** reconnectAttempt));
      const jitteredDelay = Math.round(baseDelay * (0.8 + Math.random() * 0.4));
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, jitteredDelay);
    };
    const connect = () => {
      if (
        stopped
        || socket?.readyState === WebSocket.OPEN
        || socket?.readyState === WebSocket.CONNECTING
      ) return;
      try {
        const nextSocket = new WebSocket(gatewayWebSocketUrl('/notifications'));
        socket = nextSocket;
        nextSocket.onopen = () => {
          reconnectAttempt = 0;
        };
        nextSocket.onmessage = (event) => {
          try {
            const message: unknown = JSON.parse(String(event.data));
            if (typeof message !== 'object' || message === null || !('type' in message)) return;
            if (message.type === 'notifications.sync' && 'data' in message) {
              const synchronized = parseNotificationSync(message.data);
              if (synchronized) {
                setSummary({
                  ...synchronized,
                  notifications: synchronized.notifications.slice(0, limit),
                });
                setError(null);
                setIsLoading(false);
              }
            } else if (message.type === 'notification') {
              scheduleRealtimeRefresh();
            } else if (message.type === 'notifications.sync_error') {
              void refresh();
            }
          } catch {
            // Ignore malformed server messages; REST remains the source of truth.
          }
        };
        nextSocket.onerror = () => {
          nextSocket.close();
        };
        nextSocket.onclose = () => {
          if (socket === nextSocket) socket = null;
          scheduleReconnect();
        };
      } catch {
        socket = null;
        scheduleReconnect();
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        announceNotificationsChanged('realtime');
        connect();
      }
    };
    const onFocus = () => {
      announceNotificationsChanged('realtime');
      connect();
    };
    const onOnline = () => {
      announceNotificationsChanged('realtime');
      connect();
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
        void refresh();
      }
    }, 60_000);

    connect();
    window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
    window.addEventListener('focus', onFocus);
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      controller.abort();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (realtimeRefreshTimer !== null) window.clearTimeout(realtimeRefreshTimer);
      window.clearInterval(fallbackInterval);
      socket?.close();
      socket = null;
      window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [limit, refresh]);

  return { ...summary, isLoading, error, refresh };
}
