import { headers } from 'next/headers';
import { NextResponse } from 'next/server';

import { getAuth } from '@/lib/auth';
import { getDb } from '@/lib/db';
import { requireWorkspaceSession, WorkspaceSessionError } from '@/lib/workspace-session';

export const dynamic = 'force-dynamic';

const CONFIG_ID = 'certus-workspace';
const allowedScopes = new Set(['read', 'write', 'admin']);
const allowedExpiryDays = new Set([30, 90, 365]);

function errorResponse(error: unknown) {
  if (error instanceof WorkspaceSessionError) {
    return NextResponse.json(
      { error: error.code, message: error.message },
      { status: error.status },
    );
  }
  console.error('API key operation failed.', error);
  return NextResponse.json(
    { error: 'ApiKeyOperationFailed', message: 'API keys are temporarily unavailable.' },
    { status: 500, headers: { 'Cache-Control': 'no-store' } },
  );
}

export async function GET() {
  try {
    const requestHeaders = await headers();
    const { tenantId } = await requireWorkspaceSession(requestHeaders);
    const result = await getDb().query<{
      id: string;
      name: string | null;
      start: string | null;
      enabled: boolean;
      request_count: number;
      last_request: Date | null;
      expires_at: Date | null;
      created_at: Date;
      scopes: string[];
    }>(
      `SELECT key.id, key.name, key.start, key.enabled,
              key."requestCount" AS request_count,
              key."lastRequest" AS last_request,
              key."expiresAt" AS expires_at,
              key."createdAt" AS created_at,
              attribution.scopes
       FROM apikey AS key
       JOIN certus_api_keys AS attribution ON attribution.key_id = key.id
       WHERE attribution.tenant_id = $1 AND key."configId" = $2
       ORDER BY key."createdAt" DESC`,
      [tenantId, CONFIG_ID],
    );
    return NextResponse.json(
      { api_keys: result.rows },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (error) {
    return errorResponse(error);
  }
}

export async function POST(request: Request) {
  let createdKeyId: string | null = null;
  let requestHeaders: Headers | null = null;
  try {
    requestHeaders = await headers();
    const { session, tenantId } = await requireWorkspaceSession(requestHeaders);
    const body = await request.json().catch(() => null);
    const name = typeof body?.name === 'string' ? body.name.trim() : '';
    const requestedScopes: unknown[] = Array.isArray(body?.scopes) ? body.scopes : [];
    const scopes: string[] = [
      ...new Set(requestedScopes.filter((scope: unknown): scope is string => (
          typeof scope === 'string' && allowedScopes.has(scope)
        ))),
    ];
    const expiresInDays = Number(body?.expires_in_days ?? 90);

    if (name.length < 1 || name.length > 32) {
      return NextResponse.json(
        { error: 'InvalidRequest', message: 'Key name must contain 1 to 32 characters.' },
        { status: 400 },
      );
    }
    if (scopes.length === 0 || !allowedExpiryDays.has(expiresInDays)) {
      return NextResponse.json(
        {
          error: 'InvalidRequest',
          message: 'Choose at least one valid scope and an expiry of 30, 90, or 365 days.',
        },
        { status: 400 },
      );
    }

    const created = await getAuth().api.createApiKey({
      headers: requestHeaders,
      body: {
        configId: CONFIG_ID,
        name,
        organizationId: tenantId,
        expiresIn: expiresInDays * 24 * 60 * 60,
      },
    });
    createdKeyId = created.id;
    await getDb().query(
      `INSERT INTO certus_api_keys (key_id, tenant_id, created_by, scopes)
       VALUES ($1, $2, $3, $4)`,
      [created.id, tenantId, session.user.id, scopes],
    );

    return NextResponse.json(
      {
        api_key: {
          id: created.id,
          name: created.name,
          key: created.key,
          start: created.start,
          enabled: created.enabled,
          request_count: created.requestCount,
          last_request: created.lastRequest,
          expires_at: created.expiresAt,
          created_at: created.createdAt,
          scopes,
        },
      },
      { status: 201, headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (error) {
    if (createdKeyId && requestHeaders) {
      await getAuth().api.deleteApiKey({
        headers: requestHeaders,
        body: { keyId: createdKeyId, configId: CONFIG_ID },
      }).catch(() => undefined);
    }
    return errorResponse(error);
  }
}
