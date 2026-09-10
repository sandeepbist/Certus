export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    try {
      await import('./instrumentation-node');
    } catch (error) {
      console.error('Certus Web startup validation failed.', error);
      if (process.env.NODE_ENV === 'production') {
        process.exit(1);
      }
      throw error;
    }
  }
}
