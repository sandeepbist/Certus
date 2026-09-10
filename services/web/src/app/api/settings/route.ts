import { headers } from 'next/headers';
import { NextResponse } from 'next/server';

import { getDb } from '@/lib/db';
import { requireWorkspaceSession, WorkspaceSessionError } from '@/lib/workspace-session';

export const dynamic = 'force-dynamic';

const allowedThemes = new Set(['dark', 'light', 'system']);
const allowedChunkStrategies = new Set(['token', 'sentence', 'recursive']);
const availableModels = [
  'auto',
  process.env.OPENAI_FAST_MODEL || 'gpt-5.4-mini',
  process.env.OPENAI_REASONING_MODEL || 'gpt-5.5',
];
const openAiKey = process.env.OPENAI_API_KEY?.trim().toLowerCase() || '';
const generativeProviderConfigured = Boolean(openAiKey)
  && !openAiKey.startsWith('placeholder')
  && !openAiKey.startsWith('test');
const runtimeAvailableModels = generativeProviderConfigured ? availableModels : ['auto'];

function projectRuntimeSettings(settings: Record<string, unknown> | null) {
  if (!settings || generativeProviderConfigured) return settings;
  return { ...settings, default_model: 'auto' };
}

async function loadSettings(tenantId: string, userId: string) {
  await getDb().query(
    `INSERT INTO user_preferences (user_id, organization_id)
     VALUES ($1, $2)
     ON CONFLICT (user_id) DO UPDATE
     SET organization_id = EXCLUDED.organization_id, updated_at = NOW()`,
    [userId, tenantId],
  );

  const result = await getDb().query(
    `SELECT organization.id AS organization_id,
            organization.name AS organization_name,
            organization.slug AS organization_slug,
            member.role,
            config.plan,
            config.max_documents,
            config.max_storage_bytes,
            COALESCE(usage.committed_documents, 0)::bigint AS committed_documents,
            COALESCE(usage.reserved_documents, 0)::bigint AS reserved_documents,
            COALESCE(usage.committed_original_bytes, 0)::bigint AS committed_original_bytes,
            COALESCE(usage.reserved_original_bytes, 0)::bigint AS reserved_original_bytes,
            config.max_token_budget_daily,
            preferences.theme,
            preferences.default_model,
            preferences.default_chunk_strategy,
            preferences.notification_email,
            preferences.notification_in_app,
            preferences.notification_digest
     FROM organization
     JOIN member
       ON member."organizationId" = organization.id
      AND member."userId" = $2
     LEFT JOIN tenant_config AS config
       ON config.organization_id = organization.id
     LEFT JOIN workspace_storage_usage AS usage
       ON usage.tenant_id = organization.id
     JOIN user_preferences AS preferences
       ON preferences.user_id = $2
      AND preferences.organization_id = organization.id
     WHERE organization.id = $1`,
    [tenantId, userId],
  );
  return result.rows[0] ?? null;
}

function errorResponse(error: unknown) {
  if (error instanceof WorkspaceSessionError) {
    return NextResponse.json(
      { error: error.code, message: error.message },
      { status: error.status },
    );
  }
  console.error('Settings operation failed.', error);
  return NextResponse.json(
    { error: 'SettingsOperationFailed', message: 'Settings are temporarily unavailable.' },
    { status: 500, headers: { 'Cache-Control': 'no-store' } },
  );
}

export async function GET() {
  try {
    const requestHeaders = await headers();
    const { session, tenantId } = await requireWorkspaceSession(requestHeaders);
    const settings = await loadSettings(tenantId, session.user.id);
    if (!settings) {
      return NextResponse.json(
        { error: 'NotFound', message: 'Workspace settings were not found.' },
        { status: 404 },
      );
    }
    return NextResponse.json(
      {
        settings: projectRuntimeSettings(settings),
        available_models: runtimeAvailableModels,
        generative_provider_configured: generativeProviderConfigured,
      },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (error) {
    return errorResponse(error);
  }
}

export async function PATCH(request: Request) {
  try {
    const requestHeaders = await headers();
    const { session, tenantId } = await requireWorkspaceSession(requestHeaders);
    const body = await request.json().catch(() => null);
    const theme = typeof body?.theme === 'string' ? body.theme : '';
    const defaultModel = typeof body?.default_model === 'string' ? body.default_model : '';
    const defaultChunkStrategy = typeof body?.default_chunk_strategy === 'string'
      ? body.default_chunk_strategy
      : '';
    if (
      !allowedThemes.has(theme)
      || !runtimeAvailableModels.includes(defaultModel)
      || !allowedChunkStrategies.has(defaultChunkStrategy)
      || typeof body?.notification_email !== 'boolean'
      || typeof body?.notification_in_app !== 'boolean'
      || typeof body?.notification_digest !== 'boolean'
    ) {
      return NextResponse.json(
        { error: 'InvalidRequest', message: 'One or more preference values are invalid.' },
        { status: 400 },
      );
    }

    await getDb().query(
      `INSERT INTO user_preferences (
          user_id, organization_id, theme, default_model, default_chunk_strategy,
          notification_email, notification_in_app, notification_digest
       )
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
       ON CONFLICT (user_id) DO UPDATE
       SET organization_id = EXCLUDED.organization_id,
           theme = EXCLUDED.theme,
           default_model = EXCLUDED.default_model,
           default_chunk_strategy = EXCLUDED.default_chunk_strategy,
           notification_email = EXCLUDED.notification_email,
           notification_in_app = EXCLUDED.notification_in_app,
           notification_digest = EXCLUDED.notification_digest,
           updated_at = NOW()`,
      [
        session.user.id,
        tenantId,
        theme,
        defaultModel,
        defaultChunkStrategy,
        body.notification_email,
        body.notification_in_app,
        body.notification_digest,
      ],
    );
    return NextResponse.json(
      {
        settings: projectRuntimeSettings(await loadSettings(tenantId, session.user.id)),
        available_models: runtimeAvailableModels,
        generative_provider_configured: generativeProviderConfigured,
      },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (error) {
    return errorResponse(error);
  }
}
