'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { Search, Bell, AlertCircle, Check, Inbox } from 'lucide-react';
import { useSession } from '@/lib/auth-client';
import { announceNotificationsChanged, useNotificationSummary } from '@/hooks/useNotifications';
import { gatewayFetch } from '@/lib/gateway-client';
import { OPEN_COMMAND_PALETTE_EVENT } from '@/components/shared/CommandPalette';

export function Header() {
  const { data: session } = useSession();
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [notificationActionError, setNotificationActionError] = useState<string | null>(null);
  const notificationMenu = useRef<HTMLDivElement>(null);
  const { notifications, unread_count, isLoading, error } = useNotificationSummary(5);

  useEffect(() => {
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!notificationMenu.current?.contains(event.target as Node)) {
        setNotificationsOpen(false);
      }
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    return () => document.removeEventListener('mousedown', closeOnOutsideClick);
  }, []);

  const markRead = async (id: string) => {
    try {
      const response = await gatewayFetch(`/notifications/${id}/read`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_read: true }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.message || payload?.detail || 'Notification could not be updated.');
      }
      setNotificationActionError(null);
      announceNotificationsChanged();
    } catch (mutationError) {
      setNotificationActionError(
        mutationError instanceof Error ? mutationError.message : 'Notification could not be updated.',
      );
    }
  };

  return (
    <header className="h-14 border-b border-zinc-800/80 bg-zinc-950 px-6 flex items-center justify-between shrink-0">
      {/* Search Input */}
      <div className="relative w-72">
        <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
        <input
          type="text"
          placeholder="Search Certus workspace... (⌘K)"
          className="w-full pl-9 pr-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600 transition-colors"
          onClick={() => {
            window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
          }}
          readOnly
        />
      </div>

      {/* Right Controls */}
      <div className="flex items-center gap-3">
        {/* Status */}
        <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-zinc-900 border border-zinc-800 text-[11px] text-zinc-400 font-mono">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
          <span>Certus Engine</span>
        </div>

        {/* Notifications */}
        <div className="relative" ref={notificationMenu}>
          <button
            type="button"
            aria-label={`View notifications${unread_count ? `, ${unread_count} unread` : ''}`}
            aria-expanded={notificationsOpen}
            onClick={() => setNotificationsOpen((open) => !open)}
            className="relative p-1.5 text-zinc-400 hover:text-white transition-colors"
          >
            <Bell className="w-4 h-4" />
            {unread_count > 0 && (
              <span className="absolute -right-1 -top-1 min-w-4 h-4 rounded-full bg-white px-1 text-[9px] leading-4 text-center font-semibold text-black">
                {unread_count > 99 ? '99+' : unread_count}
              </span>
            )}
          </button>

          {notificationsOpen && (
            <div className="absolute right-0 top-9 z-40 w-80 overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950 shadow-2xl">
              <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2.5">
                <div>
                  <p className="text-xs font-semibold text-white">Notifications</p>
                  <p className="text-[10px] text-zinc-500">{unread_count} unread</p>
                </div>
                <Link href="/notifications" onClick={() => setNotificationsOpen(false)} className="text-[10px] text-zinc-400 hover:text-white">
                  View all
                </Link>
              </div>

              {error || notificationActionError ? (
                <div className="flex gap-2 p-4 text-xs text-red-300"><AlertCircle className="w-4 h-4 shrink-0" />{notificationActionError || error}</div>
              ) : isLoading ? (
                <div className="p-5 text-center text-xs text-zinc-500">Loading notifications…</div>
              ) : notifications.length === 0 ? (
                <div className="p-6 text-center text-xs text-zinc-500"><Inbox className="mx-auto mb-2 w-5 h-5 text-zinc-600" />You are all caught up.</div>
              ) : (
                <div className="max-h-80 divide-y divide-zinc-800 overflow-y-auto">
                  {notifications.map((notification) => (
                    <div key={notification.id} className="flex items-start gap-2 px-3 py-3 hover:bg-zinc-900/60">
                      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400" />
                      <Link
                        href={notification.action_url || '/notifications'}
                        onClick={() => {
                          setNotificationsOpen(false);
                          void markRead(notification.id);
                        }}
                        className="min-w-0 flex-1"
                      >
                        <p className="truncate text-xs font-medium text-zinc-200">{notification.title}</p>
                        {notification.body && <p className="mt-0.5 line-clamp-2 text-[10px] leading-relaxed text-zinc-500">{notification.body}</p>}
                      </Link>
                      <button
                        type="button"
                        onClick={() => void markRead(notification.id)}
                        title="Mark read"
                        aria-label={`Mark ${notification.title} read`}
                        className="p-1 text-zinc-600 hover:text-white"
                      >
                        <Check className="w-3 h-3" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* User Avatar */}
        <div className="w-7 h-7 rounded-lg bg-zinc-900 text-zinc-200 border border-zinc-800 flex items-center justify-center font-semibold text-xs">
          {session?.user?.name?.[0]?.toUpperCase() || 'C'}
        </div>
      </div>
    </header>
  );
}
