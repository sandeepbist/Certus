import { NextResponse } from 'next/server';

import { getAuth } from '@/lib/auth';
import { createAuthMail } from '@/lib/auth-mail';
import { getDb } from '@/lib/db';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    getAuth();
    const mail = createAuthMail();
    if (mail.mode === 'smtp') await mail.assertAvailable();
    await getDb().query('SELECT id FROM auth_mail_outbox LIMIT 0');
    return NextResponse.json(
      { status: 'ready', service: 'web', authEmail: mail.mode },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch {
    return NextResponse.json(
      { status: 'not_ready', service: 'web' },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}
