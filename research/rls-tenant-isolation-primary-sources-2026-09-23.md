# PostgreSQL 17 RLS notes for tenant isolation

**Scope.** This note summarizes PostgreSQL 17 behavior from the official manual, checked 2026-09-23. Certus documents the Fastify gateway as the authentication boundary, internal services as PostgreSQL clients, and PostgreSQL as authoritative for product state ([architecture](../README.md#architecture)). These database semantics apply at the migration and service connection boundary; they do not prove that any specific Certus path enforces tenant isolation.

## RLS boundary and roles

RLS is an additional check on top of ordinary SQL privileges. A table must have `ENABLE ROW LEVEL SECURITY`; normal row access then needs both SQL privileges and an applicable policy. If RLS is enabled without an applicable policy, PostgreSQL denies row access by default. RLS does not cover whole-table `TRUNCATE` or `REFERENCES` operations. ([Row Security Policies](https://www.postgresql.org/docs/17/ddl-rowsecurity.html))

Table owners normally bypass policies. `FORCE ROW LEVEL SECURITY` subjects the owner to policies, but superusers and roles with `BYPASSRLS` still bypass RLS. Only the table owner can enable or disable RLS and create, alter, or drop its policies. A practical role split is therefore a migration/table-owner role and a runtime role that is neither owner, superuser, nor `BYPASSRLS`; this is a design recommendation derived from PostgreSQL's bypass rules, not a PostgreSQL requirement. ([Row Security Policies](https://www.postgresql.org/docs/17/ddl-rowsecurity.html), [Role Attributes](https://www.postgresql.org/docs/17/role-attributes.html))

## Policy expressions

`USING` filters existing rows available to a command. `WITH CHECK` validates proposed rows for `INSERT` and `UPDATE`; a false or null check rejects the write. For `ALL` and `UPDATE`, PostgreSQL uses `USING` as `WITH CHECK` when no separate check expression is provided. Policies default to all commands and `PUBLIC`; multiple permissive policies combine with `OR`, while restrictive policies combine with `AND` and still require at least one permissive policy to grant access. ([CREATE POLICY](https://www.postgresql.org/docs/17/sql-createpolicy.html))

## Tenant context and pooled connections

`current_setting(name, true)` reads a setting and returns `NULL` when it is absent. `set_config(name, value, true)` sets it for the current transaction; `false` makes it session-scoped. `SET LOCAL` also ends at commit or rollback and has no effect outside a transaction block. A plain `SET` made in a committed transaction persists for the rest of that database session. ([System Administration Functions](https://www.postgresql.org/docs/17/functions-admin.html), [SET](https://www.postgresql.org/docs/17/sql-set.html))

PostgreSQL accepts two-part custom setting names as placeholders, and `SET` changes settings for the current session. Therefore a policy based on an application GUC such as `app.tenant_id` uses mutable session context; it does not independently authenticate tenant identity. This conclusion follows from the documented setting behavior. For a shared database role, treat the application's identity-to-GUC assignment as trusted input. With a reused connection, set tenant context transaction-locally inside the same transaction as tenant queries, then commit or roll back before returning the connection. The pooler implication is operational guidance derived from PostgreSQL's session and transaction scope; PostgreSQL does not prescribe pool behavior. ([Customized Options](https://www.postgresql.org/docs/17/runtime-config-custom.html), [Setting Parameters](https://www.postgresql.org/docs/17/config-setting.html), [SET](https://www.postgresql.org/docs/17/sql-set.html))

## `SECURITY DEFINER`

`SECURITY DEFINER` functions execute with the function owner's privileges, so they can cross the caller's normal privilege boundary. PostgreSQL advises setting `search_path` to trusted schemas and placing `pg_temp` last. New functions grant `EXECUTE` to `PUBLIC` by default; revoke that default and grant execution selectively, ideally in the same transaction as function creation. ([CREATE FUNCTION](https://www.postgresql.org/docs/17/sql-createfunction.html))

## Performance and indexes

PostgreSQL describes policies that inspect only the row being accessed as the simplest and best-performing case. Policy expressions that query other rows can introduce race conditions; row-share locks used to address them can add cost under concurrent updates. ([Row Security Policies](https://www.postgresql.org/docs/17/ddl-rowsecurity.html))

Policy expressions are added to queries. An index on a tenant key may help when the resulting predicate is indexable and the planner estimates that index use is cheaper; PostgreSQL does not guarantee an index scan. Check plans with `EXPLAIN` against representative data after `ANALYZE`. Indexes also add write overhead. ([CREATE POLICY](https://www.postgresql.org/docs/17/sql-createpolicy.html), [Indexes: Introduction](https://www.postgresql.org/docs/17/indexes-intro.html), [Examining Index Usage](https://www.postgresql.org/docs/17/indexes-examine.html))

**Caveat.** This is a PostgreSQL behavior reference, not a Certus security audit. Actual protection and performance depend on table ownership, grants, policy definitions, function bodies, transaction boundaries, connection-pool mode, and query plans.
