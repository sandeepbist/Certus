'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutDashboard,
  MessageSquare,
  FileText,
  CheckSquare,
  Wrench,
  Activity,
  Share2,
  Zap,
  HardDrive,
  BarChart3,
  Bell,
  Settings,
  LogOut,
} from 'lucide-react';
import { signOut, useSession } from '@/lib/auth-client';

const navigationItems = [
  { name: 'Overview', href: '/dashboard', icon: LayoutDashboard },
  { name: 'Chat & Retrieval', href: '/chat', icon: MessageSquare },
  { name: 'Documents', href: '/documents', icon: FileText },
  { name: 'Knowledge Graph', href: '/graph', icon: Share2 },
  { name: 'Tasks', href: '/tasks', icon: CheckSquare },
  { name: 'Tools', href: '/tools', icon: Wrench },
  { name: 'Traces', href: '/traces', icon: Activity },
  { name: 'Automations', href: '/automations', icon: Zap },
  { name: 'Memory', href: '/memory', icon: HardDrive },
  { name: 'Analytics', href: '/analytics', icon: BarChart3 },
  { name: 'Notifications', href: '/notifications', icon: Bell },
  { name: 'Settings', href: '/settings', icon: Settings },
];

export function Sidebar({ workspaceName }: { workspaceName: string }) {
  const pathname = usePathname();
  const { data: session } = useSession();

  const handleSignOut = async () => {
    await signOut({
      fetchOptions: {
        onSuccess: () => {
          window.location.href = '/sign-in';
        },
      },
    });
  };

  return (
    <aside className="w-64 border-r border-zinc-800/80 bg-zinc-950 flex flex-col justify-between hidden md:flex shrink-0">
      {/* Brand Header */}
      <div>
        <div className="h-14 px-5 border-b border-zinc-800/80 flex items-center justify-between">
          <Link href="/dashboard" className="flex items-center gap-2">
            <span className="font-semibold text-white tracking-tight text-sm">Certus</span>
            <span className="text-[10px] px-1.5 py-0.2 rounded bg-zinc-900 text-zinc-400 font-mono border border-zinc-800">
              v1.0
            </span>
          </Link>
        </div>

        {/* Navigation Links */}
        <nav className="p-3 space-y-0.5" aria-label="Main Navigation">
          {navigationItems.map((item) => {
            const Icon = item.icon;
            const isActive =
              pathname === item.href ||
              (item.href !== '/dashboard' && pathname?.startsWith(item.href));

            return (
              <Link
                key={item.name}
                href={item.href}
                className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                  isActive
                    ? 'bg-zinc-900 text-white border border-zinc-800 font-semibold'
                    : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/50'
                }`}
              >
                <Icon className={`w-4 h-4 shrink-0 ${isActive ? 'text-white' : 'text-zinc-500'}`} />
                <span>{item.name}</span>
              </Link>
            );
          })}
        </nav>
      </div>

      {/* User / Logout */}
      <div className="p-3 border-t border-zinc-800/80">
        <div className="flex items-center justify-between px-2 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800">
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-6 h-6 rounded bg-zinc-800 text-zinc-200 border border-zinc-700 flex items-center justify-center font-bold text-[10px] shrink-0">
              {session?.user?.name?.[0]?.toUpperCase() || 'C'}
            </div>
            <div className="truncate">
              <p className="text-xs font-medium text-zinc-200 truncate">
                {session?.user?.name || 'Admin'}
              </p>
              <p className="text-[10px] text-zinc-500 font-mono truncate">
                {workspaceName}
              </p>
            </div>
          </div>

          <button
            onClick={handleSignOut}
            title="Sign Out"
            className="p-1 text-zinc-500 hover:text-zinc-200 transition-colors"
          >
            <LogOut className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </aside>
  );
}
