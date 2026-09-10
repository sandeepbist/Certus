import { Sidebar } from '@/components/shared/Sidebar';
import { Header } from '@/components/shared/Header';
import { CommandPalette } from '@/components/shared/CommandPalette';
import { getAuth } from '@/lib/auth';
import { headers } from 'next/headers';
import { redirect } from 'next/navigation';

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const requestHeaders = await headers();
  const session = await getAuth().api.getSession({ headers: requestHeaders });

  if (!session) {
    redirect('/sign-in');
  }

  if (!session.session.activeOrganizationId) {
    redirect('/onboarding');
  }

  const [member, workspace] = await Promise.all([
    getAuth().api.getActiveMember({ headers: requestHeaders }).catch(() => null),
    getAuth().api.getFullOrganization({ headers: requestHeaders }).catch(() => null),
  ]);

  if (!member || !workspace) {
    redirect('/onboarding');
  }

  return (
    <div className="min-h-screen bg-black flex text-zinc-100 selection:bg-white/20 selection:text-white">
      {/* Sidebar */}
      <Sidebar workspaceName={workspace.name} />

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-black">
        <Header />
        <main className="flex-1 p-6 md:p-8 max-w-7xl w-full mx-auto overflow-y-auto">
          {children}
        </main>
      </div>

      {/* Global Command Palette */}
      <CommandPalette />
    </div>
  );
}
