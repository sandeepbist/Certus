import os
import re
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql


MIGRATIONS = Path(__file__).resolve().parents[2] / 'infra' / 'db' / 'migrations'


@unittest.skipUnless(os.getenv('CERTUS_TEST_OWNER_DATABASE_URL'), 'set a disposable migration-owner database URL')
class AuthAccountMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = psycopg2.connect(os.environ['CERTUS_TEST_OWNER_DATABASE_URL'])
        self.addCleanup(self.connection.close)
        self.addCleanup(self.connection.rollback)
        self.cursor = self.connection.cursor()
        self.addCleanup(self.cursor.close)
        schema = 'auth_account_' + uuid4().hex
        self.cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        self.cursor.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        legacy = (MIGRATIONS / '003_better_auth.sql').read_text()
        for table in ['user', 'account']:
            definition = re.search(rf'CREATE TABLE "{table}" \(.*?\n\);', legacy, re.S)
            self.assertIsNotNone(definition)
            self.cursor.execute(definition.group())
        self.cursor.execute('CREATE UNIQUE INDEX "account_issuer_accountId_uidx" ON "account" (issuer, "accountId")')
        self.cursor.execute('''INSERT INTO "user" (id, name, email, "emailVerified")
                               VALUES ('user-1', 'First', 'first@example.invalid', FALSE),
                                      ('user-2', 'Second', 'second@example.invalid', FALSE)''')
        self.cursor.execute('''INSERT INTO "account" (id, issuer, "accountId", "providerId", "userId", password, "updatedAt")
                               VALUES ('account-1', 'old-google', 'subject', 'google', 'user-1', 'retained-hash', NOW()),
                                      ('account-2', 'old-github', 'subject', 'github', 'user-2', NULL, NOW())''')
        self.migration = (MIGRATIONS / '072_auth_account_provider_identity.sql').read_text()

    def test_preserves_legacy_accounts_and_enforces_provider_identity(self):
        self.cursor.execute('SELECT * FROM "account" ORDER BY id')
        before = self.cursor.fetchall()
        self.cursor.execute(self.migration)
        self.cursor.execute('SELECT * FROM "account" ORDER BY id')
        self.assertEqual(self.cursor.fetchall(), before)
        self.cursor.execute('''INSERT INTO "account" (id, "accountId", "providerId", "userId", "updatedAt")
                               VALUES ('new-account', 'subject', 'credential', 'user-1', NOW()) RETURNING issuer''')
        self.assertIsNone(self.cursor.fetchone()[0])
        self.cursor.execute('SAVEPOINT duplicate_identity')
        with self.assertRaises(psycopg2.errors.UniqueViolation) as rejected:
            self.cursor.execute('''INSERT INTO "account" (id, issuer, "accountId", "providerId", "userId", "updatedAt")
                                   VALUES ('duplicate', 'different-issuer', 'subject', 'google', 'user-2', NOW())''')
        self.assertEqual(rejected.exception.diag.constraint_name, 'account_providerId_accountId_uidx')
        self.cursor.execute('ROLLBACK TO SAVEPOINT duplicate_identity')

    def test_duplicate_legacy_identities_stop_the_upgrade_without_data_changes(self):
        self.cursor.execute('''INSERT INTO "account" (id, issuer, "accountId", "providerId", "userId", "updatedAt")
                               VALUES ('ambiguous', 'another-issuer', 'subject', 'google', 'user-2', NOW())''')
        self.cursor.execute('SELECT * FROM "account" ORDER BY id')
        before = self.cursor.fetchall()
        self.cursor.execute('SAVEPOINT upgrade')
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            self.cursor.execute(self.migration)
        self.cursor.execute('ROLLBACK TO SAVEPOINT upgrade')
        self.cursor.execute('SELECT * FROM "account" ORDER BY id')
        self.assertEqual(self.cursor.fetchall(), before)
        self.cursor.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'account' AND column_name = 'issuer'")
        self.assertEqual(self.cursor.fetchone()[0], 'NO')
        self.cursor.execute("SELECT to_regclass('\"account_issuer_accountId_uidx\"') IS NOT NULL")
        self.assertTrue(self.cursor.fetchone()[0])
