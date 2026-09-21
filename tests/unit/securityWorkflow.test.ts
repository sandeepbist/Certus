import { describe, expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

const repositoryRoot = path.resolve(import.meta.dir, '../..');
const securityWorkflow = readFileSync(
  path.join(repositoryRoot, '.github/workflows/security.yml'),
  'utf8',
);
const containerSecurityWorkflow = readFileSync(
  path.join(repositoryRoot, '.github/workflows/container-security.yml'),
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
    expect(securityWorkflow).toContain('inputs: requirements-dev.lock');
    expect(securityWorkflow).toContain('require-hashes: true');
    expect(securityWorkflow).toContain('disable-pip: true');
    expect(securityWorkflow).toContain('no-deps: true');

    const workflowDirectory = path.join(repositoryRoot, '.github/workflows');
    const workflows = readdirSync(workflowDirectory)
      .filter((name) => name.endsWith('.yml') || name.endsWith('.yaml'))
      .map((name) => readFileSync(path.join(workflowDirectory, name), 'utf8'))
      .join('\n');
    const actionReferences = [...workflows.matchAll(/^\s*uses:\s+[^@\s]+@([^\s#]+)/gm)];
    expect(actionReferences.length).toBeGreaterThan(0);
    for (const [, revision] of actionReferences) {
      expect(revision).toMatch(/^[a-f0-9]{40}$/);
    }
  });

  test('builds, inventories, and scans every application image', () => {
    for (const service of [
      'embedding',
      'gateway',
      'ingestion',
      'mcp-tools',
      'orchestration',
      'web',
      'workflows',
    ]) {
      expect(containerSecurityWorkflow).toContain(`- ${service}`);
    }
    expect(containerSecurityWorkflow).toContain('format: spdx-json');
    expect(containerSecurityWorkflow).toContain('severity-cutoff: high');
    expect(containerSecurityWorkflow).toContain('only-fixed: false');
    expect(containerSecurityWorkflow).toContain('only-fixed: true');
    expect(containerSecurityWorkflow).toContain('output-format: sarif');
    expect(containerSecurityWorkflow).toContain("github.actor != 'dependabot[bot]'");
    expect(containerSecurityWorkflow).toContain(
      'github.event.pull_request.head.repo.full_name == github.repository',
    );
    expect(containerSecurityWorkflow).toContain('retention-days: 14');
    expect(containerSecurityWorkflow).toContain('schedule:');
  });
});
