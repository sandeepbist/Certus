import { describe, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const migrationScript = readFileSync(
  path.resolve(import.meta.dir, '../../scripts/migrate-db.sh'),
  'utf8',
);

describe('PostgreSQL function EXECUTE ACLs', () => {
  test('revokes PUBLIC from migration-owned application functions only', () => {
    const revokeQuery = migrationScript.match(
      /SELECT format\(\s*'REVOKE EXECUTE ON FUNCTION[\s\S]*?\\gexec/,
    )?.[0];

    expect(revokeQuery).toBeDefined();
    expect(revokeQuery).toContain("namespace.nspname = 'public'");
    expect(revokeQuery).toContain("routine.proowner = :'certus_migration_role'::regrole");
    expect(revokeQuery).toContain("dependency.classid = 'pg_proc'::regclass");
    expect(revokeQuery).toContain("dependency.refclassid = 'pg_extension'::regclass");
    expect(revokeQuery).toContain("dependency.deptype = 'e'");
  });

  test('keeps explicit runtime EXECUTE and blocks PUBLIC on future functions', () => {
    expect(migrationScript).toContain(
      "'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO %I'",
    );
    expect(migrationScript).toContain(
      'ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;',
    );
    expect(migrationScript).toContain(
      'ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO :"certus_runtime_user";',
    );
  });
});
