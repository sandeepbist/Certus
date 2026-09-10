import { authClient } from './auth-client';

function createWorkspaceSlug(name: string) {
  const base = name
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 36) || 'workspace';
  const suffix = crypto.randomUUID().replaceAll('-', '').slice(0, 10);

  return `${base}-${suffix}`;
}

export async function createWorkspace(name: string) {
  const workspaceName = name.trim();

  if (workspaceName.length < 2 || workspaceName.length > 80) {
    throw new Error('Workspace name must be between 2 and 80 characters.');
  }

  const created = await authClient.organization.create({
    name: workspaceName,
    slug: createWorkspaceSlug(workspaceName),
  });

  if (created.error || !created.data) {
    throw new Error(created.error?.message || 'Could not create the workspace.');
  }

  const activated = await authClient.organization.setActive({
    organizationId: created.data.id,
  });

  if (activated.error) {
    throw new Error(activated.error.message || 'Could not activate the workspace.');
  }

  return created.data;
}
