'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bell,
  Check,
  CheckCheck,
  Circle,
  ExternalLink,
  Inbox,
  Trash2,
} from 'lucide-react';

import {
  announceNotificationsChanged,
  NOTIFICATIONS_CHANGED_EVENT,
  NotificationItem,
} from '@/hooks/useNotifications';
import { gatewayFetch } from '@/lib/gateway-client';

type NotificationResponse = {
  notifications: NotificationItem[];
  unread_count: number;
  available_types: string[];
  pagination: {
    limit: number;
    next_cursor: string | null;
  };
};

const PAGE_SIZE = 25;

function displayType(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatCreatedAt(value: string) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return 'Unknown time';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(timestamp);
}

export default function NotificationsPage() {
  const [data, setData] = useState<NotificationResponse | null>(null);
  const [status, setStatus] = useState<'all' | 'unread' | 'read'>('all');
  const [notificationType, setNotificationType] = useState('all');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [isMutating, setIsMutating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadNotifications = useCallback(async (pageCursor?: string, signal?: AbortSignal) => {
    if (pageCursor) setIsLoadingMore(true);
    else setIsLoading(true);
    setError(null);
    try {
      const query = new URLSearchParams({
        status,
        limit: String(PAGE_SIZE),
      });
      if (notificationType !== 'all') query.set('type', notificationType);
      if (pageCursor) query.set('cursor', pageCursor);
      const response = await gatewayFetch(`/notifications?${query}`, { signal });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.message || payload?.detail || 'Notifications could not be loaded.');
      }
      const page = payload as NotificationResponse;
      setData((current) => {
        if (!pageCursor || !current) return page;
        const existingIds = new Set(current.notifications.map((notification) => notification.id));
        return {
          ...page,
          notifications: [
            ...current.notifications,
            ...page.notifications.filter((notification) => !existingIds.has(notification.id)),
          ],
        };
      });
      if (!pageCursor) setSelected(new Set());
    } catch (loadError) {
      if (loadError instanceof DOMException && loadError.name === 'AbortError') return;
      if (!pageCursor) setData(null);
      setError(loadError instanceof Error ? loadError.message : 'Notifications could not be loaded.');
    } finally {
      if (pageCursor) setIsLoadingMore(false);
      else setIsLoading(false);
    }
  }, [notificationType, status]);

  useEffect(() => {
    const controller = new AbortController();
    void loadNotifications(undefined, controller.signal);
    return () => controller.abort();
  }, [loadNotifications]);

  useEffect(() => {
    const onNotificationsChanged = () => void loadNotifications();
    window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onNotificationsChanged);
    return () => window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onNotificationsChanged);
  }, [loadNotifications]);

  const mutate = async (request: () => Promise<Response>) => {
    setIsMutating(true);
    setError(null);
    try {
      const response = await request();
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.message || payload?.detail || 'The notification update failed.');
      }
      await loadNotifications();
      announceNotificationsChanged();
    } catch (mutationError) {
      setError(mutationError instanceof Error ? mutationError.message : 'The notification update failed.');
    } finally {
      setIsMutating(false);
    }
  };

  const changeReadState = (notification: NotificationItem) => mutate(() => (
    gatewayFetch(`/notifications/${notification.id}/read`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_read: !notification.is_read }),
    })
  ));

  const deleteNotification = (notification: NotificationItem) => mutate(() => (
    gatewayFetch(`/notifications/${notification.id}`, { method: 'DELETE' })
  ));

  const bulkAction = (operation: 'read' | 'unread' | 'delete') => mutate(() => (
    gatewayFetch('/notifications/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation, ids: [...selected] }),
    })
  ));

  const markAllRead = () => mutate(() => (
    gatewayFetch('/notifications/read-all', { method: 'POST' })
  ));

  const pageIds = useMemo(() => data?.notifications.map((notification) => notification.id) ?? [], [data]);
  const pageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  const togglePage = () => {
    setSelected(pageSelected ? new Set() : new Set(pageIds));
  };

  const toggleSelection = (id: string) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-5">
      <div className="pb-4 border-b border-zinc-800 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">Notifications</h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            Durable alerts from reminders, automations, and workspace activity.
          </p>
        </div>
        <button
          type="button"
          onClick={markAllRead}
          disabled={isMutating || !data?.unread_count}
          className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg border border-zinc-800 bg-zinc-900 text-xs text-zinc-300 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <CheckCheck className="w-3.5 h-3.5" />
          Mark all read
        </button>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Summary label="Loaded" value={data?.notifications.length ?? 0} icon={Bell} />
        <Summary label="Unread" value={data?.unread_count ?? 0} icon={Circle} />
        <Summary label="Selected" value={selected.size} icon={Check} className="col-span-2 sm:col-span-1" />
      </div>

      <div className="flex flex-col sm:flex-row gap-2.5">
        <div className="flex rounded-lg border border-zinc-800 bg-zinc-950 p-1">
          {(['all', 'unread', 'read'] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => {
                setStatus(value);
              }}
              className={`px-3 py-1.5 rounded-md text-xs capitalize transition-colors ${
                status === value ? 'bg-zinc-800 text-white' : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {value}
            </button>
          ))}
        </div>
        <select
          value={notificationType}
          onChange={(event) => {
            setNotificationType(event.target.value);
          }}
          aria-label="Filter notification type"
          className="px-3 py-2 rounded-lg border border-zinc-800 bg-zinc-950 text-xs text-zinc-300"
        >
          <option value="all">All types</option>
          {data?.available_types.map((type) => (
            <option key={type} value={type}>{displayType(type)}</option>
          ))}
        </select>
      </div>

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2">
          <span className="mr-auto text-xs text-zinc-400">{selected.size} selected</span>
          <BulkButton label="Mark read" onClick={() => bulkAction('read')} disabled={isMutating} />
          <BulkButton label="Mark unread" onClick={() => bulkAction('unread')} disabled={isMutating} />
          <BulkButton label="Delete" onClick={() => bulkAction('delete')} disabled={isMutating} destructive />
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
        {data && data.notifications.length > 0 && (
          <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-2 text-[11px] text-zinc-500">
            <input
              type="checkbox"
              checked={pageSelected}
              onChange={togglePage}
              aria-label="Select all loaded notifications"
              className="accent-white"
            />
            Select loaded notifications
          </div>
        )}

        {isLoading ? (
          <div className="p-10 text-center text-xs text-zinc-500">Loading notifications…</div>
        ) : !data || data.notifications.length === 0 ? (
          <div className="p-10 text-center">
            <Inbox className="mx-auto w-6 h-6 text-zinc-600" />
            <p className="mt-3 text-sm text-zinc-300">No matching notifications</p>
            <p className="mt-1 text-xs text-zinc-600">New workspace activity will appear here.</p>
          </div>
        ) : (
          <div className="divide-y divide-zinc-800">
            {data.notifications.map((notification) => (
              <article
                key={notification.id}
                className={`flex items-start gap-3 px-4 py-4 ${notification.is_read ? 'bg-zinc-950' : 'bg-zinc-900/45'}`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(notification.id)}
                  onChange={() => toggleSelection(notification.id)}
                  aria-label={`Select ${notification.title}`}
                  className="mt-1 accent-white"
                />
                <span className={`mt-1.5 w-2 h-2 shrink-0 rounded-full ${notification.is_read ? 'bg-zinc-700' : 'bg-emerald-400'}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className={`text-xs ${notification.is_read ? 'font-medium text-zinc-300' : 'font-semibold text-white'}`}>
                      {notification.title}
                    </h2>
                    <span className="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-500">
                      {displayType(notification.type)}
                    </span>
                  </div>
                  {notification.body && <p className="mt-1 text-xs leading-relaxed text-zinc-400">{notification.body}</p>}
                  <div className="mt-2 flex flex-wrap items-center gap-3 text-[10px] text-zinc-600">
                    <time dateTime={notification.created_at}>{formatCreatedAt(notification.created_at)}</time>
                    {notification.action_url && (
                      <Link href={notification.action_url} className="inline-flex items-center gap-1 text-zinc-400 hover:text-white">
                        Open related item <ExternalLink className="w-3 h-3" />
                      </Link>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    onClick={() => changeReadState(notification)}
                    disabled={isMutating}
                    title={notification.is_read ? 'Mark unread' : 'Mark read'}
                    aria-label={notification.is_read ? 'Mark unread' : 'Mark read'}
                    className="p-1.5 text-zinc-500 hover:text-white disabled:opacity-40"
                  >
                    {notification.is_read ? <Circle className="w-3.5 h-3.5" /> : <Check className="w-3.5 h-3.5" />}
                  </button>
                  <button
                    type="button"
                    onClick={() => deleteNotification(notification)}
                    disabled={isMutating}
                    title="Delete notification"
                    aria-label="Delete notification"
                    className="p-1.5 text-zinc-500 hover:text-red-400 disabled:opacity-40"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      {data?.pagination.next_cursor && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => void loadNotifications(data.pagination.next_cursor || undefined)}
            disabled={isLoadingMore}
            className="rounded-lg border border-zinc-800 bg-zinc-950 px-4 py-2 text-xs text-zinc-300 hover:text-white disabled:opacity-40"
          >
            {isLoadingMore ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </div>
  );
}

function Summary({ label, value, icon: Icon, className = '' }: { label: string; value: number; icon: React.ComponentType<{ className?: string }>; className?: string }) {
  return (
    <div className={`rounded-xl border border-zinc-800 bg-zinc-950 p-4 ${className}`}>
      <div className="flex items-center justify-between text-xs text-zinc-500"><span>{label}</span><Icon className="w-3.5 h-3.5" /></div>
      <p className="mt-1 text-xl font-semibold font-mono text-white">{value.toLocaleString()}</p>
    </div>
  );
}

function BulkButton({ label, onClick, disabled, destructive = false }: { label: string; onClick: () => void; disabled: boolean; destructive?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md border px-2.5 py-1 text-[11px] disabled:opacity-40 ${
        destructive
          ? 'border-red-950 bg-red-950/30 text-red-300 hover:bg-red-950/60'
          : 'border-zinc-800 bg-zinc-900 text-zinc-300 hover:text-white'
      }`}
    >
      {label}
    </button>
  );
}
