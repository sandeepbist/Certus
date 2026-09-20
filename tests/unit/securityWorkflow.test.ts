import { describe, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const repositoryRoot = path.resolve(import.meta.dir, '../..');
const securityWorkflow = readFileSync(
  path.join(repositoryRoot, '.github/workflows/security.yml'),
  'utf8',
);

describe('repository security workflow', () => {
  test('analyzes every first-party source and workflow language with extended queries', () => {
    expect(securityWorkflow).toContain('- actions');
    expect(securityWorkflow).toContain('- javascript-typescript');
    expect(securityWorkflow).toContain('- python');
    expect(securityWorkflow).toContain('queries: security-extended');
    expect(securityWorkflow).toContain('security-events: write');
    expect(securityWorkflow).toContain('schedule:');
  });

  test('blocks vulnerable dependency additions and pins every action by commit', () => {
    expect(securityWorkflow).toContain('fail-on-severity: moderate');

    const actionReferences = [...securityWorkflow.matchAll(/^\s*uses:\s+[^@\s]+@([^\s#]+)/gm)];
    expect(actionReferences.length).toBeGreaterThan(0);
    for (const [, revision] of actionReferences) {
      expect(revision).toMatch(/^[a-f0-9]{40}$/);
    }
  });
});
