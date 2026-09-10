import type { FastifyReply, FastifyRequest } from 'fastify';

export function sendServiceUnavailable(
  request: FastifyRequest,
  reply: FastifyReply,
  serviceName: string,
  publicMessage: string,
  error: unknown,
) {
  request.log.error({ err: error, service: serviceName }, 'Internal service request failed');
  return reply.status(503).send({
    error: 'Service Unavailable',
    message: publicMessage,
  });
}
