const SAFE_IDENTIFIER = /[^A-Za-z0-9_. -]+/g;

function safeIdentifier(value: string, fallback: string) {
  const sanitized = value.replace(SAFE_IDENTIFIER, '_').replace(/^[ ._]+|[ ._]+$/g, '').slice(0, 80);
  return sanitized || fallback;
}

export function safeErrorType(error: unknown): string {
  try {
    if (error instanceof Error) {
      const prototype = Object.getPrototypeOf(error) as { constructor?: { name?: unknown } } | null;
      const constructorName = prototype?.constructor?.name;
      if (typeof constructorName === 'string') {
        return safeIdentifier(constructorName, 'Error');
      }
      return 'Error';
    }
  } catch {
    return 'UnknownError';
  }

  if (error === null) return 'ThrownNull';
  if (error === undefined) return 'ThrownUndefined';
  const primitiveType = typeof error;
  return `Thrown${primitiveType.charAt(0).toUpperCase()}${primitiveType.slice(1)}`;
}

export function safeErrorFields(error: unknown, operation: string) {
  return {
    operation: safeIdentifier(operation, 'operation'),
    errorType: safeErrorType(error),
  };
}
