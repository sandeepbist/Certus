"""PostgreSQL checks for the memories row-security pilot.

Run after migrations on a disposable database:
    CERTUS_TEST_DATABASE_URL=... \
    CERTUS_TEST_OWNER_DATABASE_URL=... \
    python3 -m unittest tests.python.test_memory_rls_database
"""

import os
import unittest
from uuid import uuid4

import psycopg2
from psycopg2 import errors

EMBEDDING_PROFILE = "embedding-space:v1:local:local-lexical-v2:1536"


class MemoryRlsDatabaseTests(unittest.TestCase):
    @staticmethod
    def _set_scope(cursor, tenant_id, user_id):
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true), "
            "set_config('app.user_id', %s, true)",
            (tenant_id, user_id),
        )

    def _assert_rejected(self, cursor, error_type, statement, parameters):
        cursor.execute("SAVEPOINT expected_rejection")
        try:
            with self.assertRaises(error_type):
                cursor.execute(statement, parameters)
        finally:
            cursor.execute("ROLLBACK TO SAVEPOINT expected_rejection")
            cursor.execute("RELEASE SAVEPOINT expected_rejection")

    @unittest.skipUnless(
        os.getenv("CERTUS_TEST_DATABASE_URL"),
        "set CERTUS_TEST_DATABASE_URL to a migrated disposable database",
    )
    def test_runtime_scope_fences_reads_writes_references_and_triggers(self):
        connection = psycopg2.connect(os.environ["CERTUS_TEST_DATABASE_URL"])
        marker = str(uuid4())
        tenant_a, user_a = f"rls-a-{marker}", f"user-a-{marker}"
        tenant_b, user_b = f"rls-b-{marker}", f"user-b-{marker}"

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT role.rolbypassrls,
                           relation.relowner = role.oid,
                           relation.relrowsecurity
                    FROM pg_roles AS role
                    JOIN pg_class AS relation ON relation.relname = 'memories'
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                     AND namespace.nspname = 'public'
                    WHERE role.rolname = current_user
                    """
                )
                bypasses_rls, owns_memories, rls_enabled = cursor.fetchone()
                self.assertFalse(bypasses_rls)
                self.assertFalse(owns_memories)
                self.assertTrue(rls_enabled)

                # Unset context denies writes. Empty context later denies every command.
                cursor.execute("SELECT id FROM memories LIMIT 1")
                self.assertIsNone(cursor.fetchone())
                self._assert_rejected(
                    cursor,
                    errors.InsufficientPrivilege,
                    "INSERT INTO memories "
                    "(tenant_id, user_id, fact, embedding_profile) "
                    "VALUES (%s, %s, %s, %s)",
                    (tenant_a, user_a, f"{marker}:no-context", EMBEDDING_PROFILE),
                )

                self._set_scope(cursor, tenant_a, user_a)
                cursor.execute(
                    "INSERT INTO webhooks "
                    "(user_id, organization_id, name, url, events, secret) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        user_a,
                        tenant_a,
                        "RLS test",
                        "https://example.invalid/rls-test",
                        ["memory_extracted"],
                        "test-secret",
                    ),
                )
                cursor.execute(
                    "INSERT INTO agent_runs (tenant_id, user_id, input_query) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (tenant_a, user_a, f"{marker}:run-a"),
                )
                run_a = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO memories "
                    "(tenant_id, user_id, fact, source_run_id, embedding_profile) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (tenant_a, user_a, f"{marker}:memory-a", run_a, EMBEDDING_PROFILE),
                )
                memory_a = cursor.fetchone()[0]

                self._set_scope(cursor, tenant_b, user_b)
                cursor.execute(
                    "INSERT INTO agent_runs (tenant_id, user_id, input_query) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (tenant_b, user_b, f"{marker}:run-b"),
                )
                run_b = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO memories "
                    "(tenant_id, user_id, fact, embedding_profile) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (tenant_b, user_b, f"{marker}:memory-b", EMBEDDING_PROFILE),
                )
                memory_b = cursor.fetchone()[0]

                self._set_scope(cursor, tenant_a, user_a)
                cursor.execute(
                    "SELECT count(*) FROM memories WHERE id = ANY(%s::uuid[])",
                    ([memory_a, memory_b],),
                )
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute(
                    "UPDATE memories SET fact = %s WHERE id = %s",
                    (f"{marker}:memory-a-updated", memory_a),
                )
                self.assertEqual(cursor.rowcount, 1)
                self._assert_rejected(
                    cursor,
                    errors.InsufficientPrivilege,
                    "INSERT INTO memories "
                    "(tenant_id, user_id, fact, embedding_profile) "
                    "VALUES (%s, %s, %s, %s)",
                    (tenant_b, user_b, f"{marker}:wrong-write", EMBEDDING_PROFILE),
                )
                self._assert_rejected(
                    cursor,
                    errors.InsufficientPrivilege,
                    "UPDATE memories SET tenant_id = %s, user_id = %s WHERE id = %s",
                    (tenant_b, user_b, memory_a),
                )

                self._set_scope(cursor, "", "")
                cursor.execute(
                    "SELECT id FROM memories WHERE id = ANY(%s::uuid[])",
                    ([memory_a, memory_b],),
                )
                self.assertEqual(cursor.fetchall(), [])
                cursor.execute(
                    "UPDATE memories SET access_count = access_count + 1 "
                    "WHERE id = ANY(%s::uuid[])",
                    ([memory_a, memory_b],),
                )
                self.assertEqual(cursor.rowcount, 0)
                cursor.execute(
                    "DELETE FROM memories WHERE id = ANY(%s::uuid[])",
                    ([memory_a, memory_b],),
                )
                self.assertEqual(cursor.rowcount, 0)

                self._set_scope(cursor, tenant_a, user_a)
                self._assert_rejected(
                    cursor,
                    errors.ForeignKeyViolation,
                    "INSERT INTO memories "
                    "(tenant_id, user_id, fact, source_run_id, embedding_profile) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (tenant_a, user_a, f"{marker}:bad-run-ref", run_b, EMBEDDING_PROFILE),
                )
                self._assert_rejected(
                    cursor,
                    errors.ForeignKeyViolation,
                    "UPDATE memories SET superseded_by = %s WHERE id = %s",
                    (memory_b, memory_a),
                )

                cursor.execute(
                    "SELECT count(*) FROM webhook_events "
                    "WHERE organization_id = %s AND user_id = %s "
                    "AND event_type = 'memory_extracted' "
                    "AND payload ->> 'memory_id' = %s",
                    (tenant_a, user_a, str(memory_a)),
                )
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute(
                    "SELECT count(*) FROM notifications "
                    "WHERE organization_id = %s AND user_id = %s "
                    "AND metadata ->> 'memory_id' = %s",
                    (tenant_a, user_a, str(memory_a)),
                )
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute(
                    "SELECT count(*) FROM notification_events "
                    "WHERE organization_id = %s AND user_id = %s "
                    "AND payload #>> '{metadata,memory_id}' = %s",
                    (tenant_a, user_a, str(memory_a)),
                )
                self.assertEqual(cursor.fetchone()[0], 1)

                cursor.execute("DELETE FROM agent_runs WHERE id = %s", (run_a,))
                cursor.execute(
                    "SELECT source_run_id, tenant_id, user_id FROM memories WHERE id = %s",
                    (memory_a,),
                )
                self.assertEqual(cursor.fetchone(), (None, tenant_a, user_a))
                cursor.execute("DELETE FROM memories WHERE id = %s", (memory_a,))
                self.assertEqual(cursor.rowcount, 1)

                self._set_scope(cursor, tenant_b, user_b)
                cursor.execute("DELETE FROM memories WHERE id = %s", (memory_b,))
                self.assertEqual(cursor.rowcount, 1)
        finally:
            connection.rollback()
            connection.close()

    @unittest.skipUnless(
        os.getenv("CERTUS_TEST_OWNER_DATABASE_URL"),
        "set CERTUS_TEST_OWNER_DATABASE_URL to test migration-owner backfills",
    )
    def test_migration_owner_can_backfill_without_runtime_context(self):
        connection = psycopg2.connect(os.environ["CERTUS_TEST_OWNER_DATABASE_URL"])
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT relation.relowner = role.oid, relation.relrowsecurity
                    FROM pg_roles AS role
                    JOIN pg_class AS relation ON relation.relname = 'memories'
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                     AND namespace.nspname = 'public'
                    WHERE role.rolname = current_user
                    """
                )
                owns_memories, rls_enabled = cursor.fetchone()
                self.assertTrue(owns_memories)
                self.assertTrue(rls_enabled)

                memory_ids = []
                for tenant in ("owner-a", "owner-b"):
                    cursor.execute(
                        "INSERT INTO memories "
                        "(tenant_id, user_id, fact, embedding_profile) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (
                            f"{tenant}-{uuid4()}",
                            "migration-backfill",
                            "before backfill",
                            EMBEDDING_PROFILE,
                        ),
                    )
                    memory_ids.append(cursor.fetchone()[0])

                cursor.execute(
                    "SELECT count(*) FROM memories WHERE id = ANY(%s::uuid[])",
                    (memory_ids,),
                )
                self.assertEqual(cursor.fetchone()[0], 2)
                cursor.execute(
                    "UPDATE memories SET fact = 'after backfill' WHERE id = %s",
                    (memory_ids[0],),
                )
                self.assertEqual(cursor.rowcount, 1)
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
