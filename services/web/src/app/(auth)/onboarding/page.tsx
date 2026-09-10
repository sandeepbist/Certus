import { headers } from 'next/headers';
import { redirect } from 'next/navigation';
import { WorkspaceSetupForm } from '@/components/auth/WorkspaceSetupForm';
import { getAuth } from '@/lib/auth';

export const dynamic = 'force-dynamic';

export default async function OnboardingPage() {
  const session = await getAuth().api.getSession({ headers: await headers() });

  if (!session) {
    redirect('/sign-in');
  }

  if (session.session.activeOrganizationId) {
    redirect('/dashboard');
  }

  return <WorkspaceSetupForm defaultName={`${session.user.name}'s Workspace`} />;
}
