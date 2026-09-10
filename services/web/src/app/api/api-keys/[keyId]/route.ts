import { headers } from 'next/headers';
import { NextResponse } from 'next/server';

import { getAuth } from '@/lib/auth';
import { getDb } from '@/lib/db';
import { requireWorkspaceSession, WorkspaceSessionError } from '@/lib/workspace-session';

const CONFIG_ID = 'certus-workspace';

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ keyId: string }> },
) {
  try {
    const requestHeaders = await headers();
    const { tenantId } = await requireWorkspaceSession(requestHeaders);
    const { keyId } = await context.params;
    const owned = await getDb().query(
      'SELECT 1 FROM certus_api_keys WHERE key_id = $1 AND tenant_id = $2',
      [keyId, tenantId],
    );
    if (owned.rowCount !== 1) {
      return NextResponse.json({ error: 'NotFound', message: 'API key not found.' }, { status: 404 });
    }

    await getAuth().api.deleteApiKey({
      headers: requestHeaders,
      body: { keyId, configId: CONFIG_ID },
    });
    return new NextResponse(null, { status: 204 });
  } catch (error) {
    if (error instanceof WorkspaceSessionError) {
      return NextResponse.json(
        { error: error.code, message: error.message },
        { status: error.status },
      );
    }
    console.error('API key revocation failed.', error);
    return NextResponse.json(
      { error: 'ApiKeyOperationFailed', message: 'API keys are temporarily unavailable.' },
      { status: 500, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}
