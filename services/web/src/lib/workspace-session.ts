import { getAuth } from './auth';

export class WorkspaceSessionError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export async function requireWorkspaceSession(requestHeaders: Headers) {
  const session = await getAuth().api.getSession({ headers: requestHeaders });
  if (!session) {
    throw new WorkspaceSessionError(401, 'Unauthorized', 'A valid Certus session is required.');
  }

  if (!session.user.emailVerified) {
    throw new WorkspaceSessionError(403, 'EmailNotVerified', 'Verify your email before accessing a workspace.');
  }

  const tenantId = session.session.activeOrganizationId;
  if (!tenantId) {
    throw new WorkspaceSessionError(403, 'WorkspaceRequired', 'Select or create a workspace first.');
  }

  const member = await getAuth().api.getActiveMember({ headers: requestHeaders }).catch(() => null);
  if (!member || member.organizationId !== tenantId || member.userId !== session.user.id) {
    throw new WorkspaceSessionError(403, 'Forbidden', 'Workspace membership could not be verified.');
  }

  return { session, tenantId, member };
}
