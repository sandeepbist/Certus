import { NextResponse } from 'next/server';

import { getAuth } from '@/lib/auth';
import { getDb } from '@/lib/db';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    getAuth();
    await getDb().query('SELECT 1 AS ready');
    return NextResponse.json(
      { status: 'ready', service: 'web' },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch {
    return NextResponse.json(
      { status: 'not_ready', service: 'web' },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}
