import type { FastifyReply, FastifyRequest } from 'fastify';
import { safeErrorFields } from './safeErrors.js';

export function sendServiceUnavailable(
  request: FastifyRequest,
  reply: FastifyReply,
  serviceName: string,
  publicMessage: string,
  error: unknown,
) {
  request.log.error(
    { ...safeErrorFields(error, 'internal service request'), service: serviceName },
    'Internal service request failed',
  );
  return reply.status(503).send({
    error: 'Service Unavailable',
    message: publicMessage,
  });
}
