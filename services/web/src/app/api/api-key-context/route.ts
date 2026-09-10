import { timingSafeEqual } from 'node:crypto';
import { NextResponse } from 'next/server';

import { getAuth } from '@/lib/auth';
import { getDb } from '@/lib/db';
import { remainingDailyTokenBudget } from '@/lib/token-budget';

export const dynamic = 'force-dynamic';

const CONFIG_ID = 'certus-workspace';

function internalRequestIsTrusted(request: Request) {
  const expected = process.env.INTERNAL_SERVICE_TOKEN?.trim();
  const provided = request.headers.get('x-internal-service-token')?.trim();
  if (!expected || !provided) return false;
  const expectedBytes = Buffer.from(expected);
  const providedBytes = Buffer.from(provided);
  return expectedBytes.length === providedBytes.length && timingSafeEqual(expectedBytes, providedBytes);
}

export async function POST(request: Request) {
  if (!internalRequestIsTrusted(request)) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const body = await request.json().catch(() => null);
  const key = typeof body?.key === 'string' ? body.key.trim() : '';
  if (!key.startsWith('certus_') || key.length > 256) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const verified = await getAuth().api.verifyApiKey({
    body: { key, configId: CONFIG_ID },
  }).catch(() => null);
  if (!verified?.valid || !verified.key) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const context = await getDb().query<{
    user_id: string;
    tenant_id: string;
    scopes: string[];
    role: string;
  }>(
    `SELECT attribution.created_by AS user_id,
            attribution.tenant_id,
            attribution.scopes,
            member.role
     FROM certus_api_keys AS attribution
     JOIN member
       ON member."organizationId" = attribution.tenant_id
      AND member."userId" = attribution.created_by
     WHERE attribution.key_id = $1
       AND attribution.tenant_id = $2`,
    [verified.key.id, verified.key.referenceId],
  );
  const value = context.rows[0];
  if (!value || value.scopes.length === 0 || !verified.key.expiresAt) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const tokenBudget = await remainingDailyTokenBudget(value.tenant_id, value.user_id);

  return NextResponse.json(
    {
      userId: value.user_id,
      tenantId: value.tenant_id,
      roles: value.role.split(',').map((role) => role.trim()).filter(Boolean),
      authType: 'api_key',
      scopes: value.scopes,
      tokenBudget,
      expiresAt: verified.key.expiresAt,
    },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}
