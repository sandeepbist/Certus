import Link from 'next/link';
import { getAuth } from '@/lib/auth';
import { ResetPasswordForm } from '@/components/auth/ResetPasswordForm';

export const dynamic = 'force-dynamic';

export default async function ResetPasswordPage({ searchParams }: {
  searchParams: Promise<{ token?: string; error?: string }>;
}) {
  const { token, error } = await searchParams;
  const context = await getAuth().$context;
  const verification = token && !error
    ? await context.internalAdapter.findVerificationValue(`reset-password:${token}`) : null;
  if (!token || !verification || verification.expiresAt <= new Date()) {
    return <div className="max-w-sm text-center space-y-4">
      <h1 className="text-lg font-semibold">Invalid or expired reset link</h1>
      <p role="alert">Request a new recovery email to reset your password.</p>
      <Link href="/forgot-password" className="underline">Request recovery email</Link>
    </div>;
  }
  return <ResetPasswordForm token={token} />;
}
