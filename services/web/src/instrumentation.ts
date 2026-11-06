import { safeErrorFields } from './lib/safe-errors';

export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    try {
      const runtime = await import('./instrumentation-node');
      await runtime.startAuthMailDelivery();
    } catch (error) {
      console.error(
        'Certus Web startup validation failed.',
        safeErrorFields(error, 'Web startup validation'),
      );
      if (process.env.NODE_ENV === 'production') {
        process.exit(1);
      }
      throw error;
    }
  }
}
