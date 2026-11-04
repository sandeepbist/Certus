import { redirect } from 'next/navigation';
import { VerifyEmailForm } from '@/components/auth/VerifyEmailForm';

export const dynamic = 'force-dynamic';

export default async function VerifyEmailPage({ searchParams }: {
  searchParams: Promise<{ token?: string; verified?: string; error?: string }>;
}) {
  const { token, verified, error } = await searchParams;
  if (token) {
    redirect(`/api/auth/verify-email?${new URLSearchParams({ token, callbackURL: '/verify-email?verified=1' })}`);
  }
  return <VerifyEmailForm verified={verified === '1' && !error} invalid={Boolean(error)} />;
}
