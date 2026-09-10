import { FastifyRequest } from 'fastify';
import { AuthUserContext } from '../types/index.js';

export function requireAuthContext(request: FastifyRequest): AuthUserContext {
  if (!request.user) {
    throw new Error('Authenticated route executed without an authentication context.');
  }

  return request.user;
}
