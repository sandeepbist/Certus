import { NextResponse } from 'next/server';
import { headers } from 'next/headers';
import { getAuth } from '@/lib/auth';
import { remainingDailyTokenBudget } from '@/lib/token-budget';

export const dynamic = 'force-dynamic';

export async function GET() {
  const requestHeaders = await headers();
  const session = await getAuth().api.getSession({ headers: requestHeaders });

  if (!session) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const tenantId = session.session.activeOrganizationId;
  if (!tenantId) {
    return NextResponse.json(
      { error: 'WorkspaceRequired', message: 'Select or create a workspace first.' },
      { status: 403 },
    );
  }

  const member = await getAuth().api.getActiveMember({ headers: requestHeaders }).catch(() => null);
  if (!member || member.organizationId !== tenantId || member.userId !== session.user.id) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 });
  }

  const tokenBudget = await remainingDailyTokenBudget(tenantId, session.user.id);

  return NextResponse.json(
    {
      userId: session.user.id,
      tenantId,
      roles: member.role.split(',').map((role) => role.trim()).filter(Boolean),
      authType: 'session',
      scopes: ['admin'],
      tokenBudget,
      expiresAt: session.session.expiresAt,
    },
    {
      headers: {
        'Cache-Control': 'no-store',
      },
    },
  );
}
