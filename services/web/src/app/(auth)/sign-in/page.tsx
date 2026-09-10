import { SignInForm } from '@/components/auth/SignInForm';

export default function SignInPage() {
  const providers: Array<'google' | 'github'> = [];
  if (process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET) providers.push('google');
  if (process.env.GITHUB_CLIENT_ID && process.env.GITHUB_CLIENT_SECRET) providers.push('github');
  return <SignInForm providers={providers} />;
}
