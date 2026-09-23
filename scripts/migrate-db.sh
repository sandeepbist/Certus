#!/usr/bin/env bash
# ============================================================
# Nexus AI-OS — Database Migration Runner
# ============================================================

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"
# shellcheck source=scripts/lib/env-file.sh
source "$repository_root/scripts/lib/env-file.sh"

certus_export_env_default DB_HOST localhost "$repository_root/.env"
certus_export_env_default DB_PORT 5432 "$repository_root/.env"
certus_export_env_default POSTGRES_USER nexus "$repository_root/.env"
certus_export_env_default POSTGRES_PASSWORD nexus_dev_password "$repository_root/.env"
certus_export_env_default POSTGRES_DB nexus "$repository_root/.env"
certus_export_env_default MIGRATION_LOCK_TIMEOUT_SECONDS 300 "$repository_root/.env"
certus_export_env_default CERTUS_RUNTIME_DB_USER certus_runtime "$repository_root/.env"
certus_export_env_default CERTUS_RUNTIME_DB_PASSWORD "" "$repository_root/.env"
runtime_database_url_default="$(python3 - <<'PY'
import os
from urllib.parse import quote, urlunsplit

host = os.environ["DB_HOST"]
if ":" in host and not host.startswith("["):
    host = f"[{host}]"
netloc = (
    f"{quote(os.environ['CERTUS_RUNTIME_DB_USER'], safe='')}:"
    f"{quote(os.environ['CERTUS_RUNTIME_DB_PASSWORD'], safe='')}@"
    f"{host}:{os.environ['DB_PORT']}"
)
database = quote(os.environ["POSTGRES_DB"], safe="")
print(urlunsplit(("postgresql", netloc, f"/{database}", "", "")))
PY
)"
certus_export_env_default DATABASE_URL "$runtime_database_url_default" "$repository_root/.env"

database_host="$DB_HOST"
database_port="$DB_PORT"
database_user="$POSTGRES_USER"
database_name="$POSTGRES_DB"
database_password="$POSTGRES_PASSWORD"
runtime_database_user="$CERTUS_RUNTIME_DB_USER"
runtime_database_password="$CERTUS_RUNTIME_DB_PASSWORD"
migration_lock_timeout_seconds="$MIGRATION_LOCK_TIMEOUT_SECONDS"

if [[ ! "$migration_lock_timeout_seconds" =~ ^[0-9]+$ ]] \
   || (( migration_lock_timeout_seconds < 1 || migration_lock_timeout_seconds > 3600 )); then
  echo "❌ MIGRATION_LOCK_TIMEOUT_SECONDS must be an integer between 1 and 3600."
  exit 1
fi
if [[ ! "$database_port" =~ ^[0-9]+$ ]] \
   || (( database_port < 1 || database_port > 65535 )); then
  echo "❌ DB_PORT must be an integer between 1 and 65535."
  exit 1
fi

if [[ ! "$runtime_database_user" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]]; then
  echo "❌ CERTUS_RUNTIME_DB_USER must be a PostgreSQL role name using letters, digits, and underscores."
  exit 1
fi
if [[ "$runtime_database_user" == "$database_user" ]]; then
  echo "❌ Runtime and migration database users must be different roles."
  exit 1
fi
if [[ -z "$runtime_database_password" ]]; then
  echo "❌ CERTUS_RUNTIME_DB_PASSWORD must be non-empty."
  exit 1
fi

export CERTUS_RUNTIME_DB_USER="$runtime_database_user"
export CERTUS_RUNTIME_DB_PASSWORD="$runtime_database_password"
export DATABASE_URL
if ! python3 - <<'PY'
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

try:
    database_url = urlsplit(os.environ["DATABASE_URL"])
    configured_user = os.environ["CERTUS_RUNTIME_DB_USER"]
    configured_password = os.environ["CERTUS_RUNTIME_DB_PASSWORD"]
    target_host = os.environ["DB_HOST"].strip("[]").lower()
    target_port = int(os.environ["DB_PORT"])
    target_database = os.environ["POSTGRES_DB"]
    query = parse_qs(database_url.query, keep_blank_values=True)
    database_path = database_url.path[1:] if database_url.path.startswith("/") else ""
    valid = (
        database_url.scheme in {"postgres", "postgresql"}
        and database_url.hostname is not None
        and database_url.hostname.lower() == target_host
        and (database_url.port or 5432) == target_port
        and unquote(database_path) == target_database
        and unquote(database_url.username or "") == configured_user
        and unquote(database_url.password or "") == configured_password
        and not any(
            key in query
            for key in ("host", "hostaddr", "port", "dbname", "user", "password", "service")
        )
    )
except (KeyError, ValueError):
    valid = False

if not valid:
    sys.exit(1)
PY
then
  echo "❌ DATABASE_URL must use the configured runtime credentials and target DB_HOST, DB_PORT, and POSTGRES_DB."
  exit 1
fi

export PGPASSWORD="$database_password"
export PGAPPNAME="certus-db-migrator"

echo "🐘 Running Certus database migrations against ${database_name}@${database_host}:${database_port}..."

# Check if psql is installed locally, else run inside docker container.
if command -v psql &> /dev/null; then
  run_psql() {
    psql -h "$database_host" -p "$database_port" -U "$database_user" -d "$database_name" -v ON_ERROR_STOP=1 "$@"
  }
elif command -v docker &> /dev/null && docker ps -q -f name=nexus-postgres | grep -q .; then
  run_psql() {
    docker exec -i \
      -e CERTUS_RUNTIME_DB_USER \
      -e CERTUS_RUNTIME_DB_PASSWORD \
      -e POSTGRES_DB \
      nexus-postgres psql -U "$database_user" -d "$database_name" -v ON_ERROR_STOP=1 "$@"
  }
else
  echo "❌ Neither local 'psql' nor running 'nexus-postgres' Docker container found."
  echo "Please start Docker with 'make docker-up' first."
  exit 1
fi

migrations_dir="$repository_root/infra/db/migrations"

checksum_file() {
  if command -v sha256sum &> /dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

shopt -s nullglob
migration_files=("$migrations_dir"/*.sql)
migration_names=()
migration_checksums=()

for migration_file in "${migration_files[@]}"; do
  migration_name="$(basename "$migration_file")"
  if [[ ! "$migration_name" =~ ^[0-9]{3}_[a-z0-9_]+\.sql$ ]]; then
    echo "❌ Invalid migration filename: $migration_name"
    exit 1
  fi

  migration_checksum="$(checksum_file "$migration_file")"
  migration_names+=("$migration_name")
  migration_checksums+=("$migration_checksum")
done

# One PostgreSQL session owns this lock for the complete release sequence.
# Each migration still commits independently, but a second release process
# cannot observe an old ledger state and race the same DDL.
{
  printf "SET statement_timeout = '%ss';\n" "$migration_lock_timeout_seconds"
  printf "SELECT pg_advisory_lock(hashtextextended('certus:schema-migrations:v1', 0));\n"
  printf "SET statement_timeout = 0;\n"
  printf '%s\n' 'CREATE TABLE IF NOT EXISTS public.schema_migrations ('
  printf '%s\n' '    filename TEXT PRIMARY KEY,'
  printf '%s\n' '    checksum TEXT NOT NULL,'
  printf '%s\n' '    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()'
  printf '%s\n' ');'

  for index in "${!migration_files[@]}"; do
    migration_file="${migration_files[$index]}"
    migration_name="${migration_names[$index]}"
    migration_checksum="${migration_checksums[$index]}"

    printf '%s\n' 'DO $certus_checksum$'
    printf '%s\n' 'BEGIN'
    printf "    IF EXISTS (SELECT 1 FROM public.schema_migrations WHERE filename = '%s' AND checksum <> '%s') THEN\n" \
      "$migration_name" "$migration_checksum"
    printf "        RAISE EXCEPTION 'Applied migration was modified: %s';\n" "$migration_name"
    printf '%s\n' '    END IF;'
    printf '%s\n' 'END'
    printf '%s\n' '$certus_checksum$;'
    printf "SELECT NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE filename = '%s') AS certus_should_apply \\gset\n" \
      "$migration_name"
    printf '%s\n' '\if :certus_should_apply'
    printf '\\echo %s\n' "  ▶ Applying $migration_name..."
    printf '%s\n' 'BEGIN;'
    sed -e '$a\' "$migration_file"
    printf "INSERT INTO public.schema_migrations (filename, checksum) VALUES ('%s', '%s');\n" \
      "$migration_name" "$migration_checksum"
    printf '%s\n' 'COMMIT;'
    printf '%s\n' '\else'
    printf '\\echo %s\n' "  ✓ Already applied $migration_name"
    printf '%s\n' '\endif'
  done

  cat <<'SQL'
BEGIN;
\getenv certus_runtime_user CERTUS_RUNTIME_DB_USER
\getenv certus_runtime_password CERTUS_RUNTIME_DB_PASSWORD
\getenv certus_database_name POSTGRES_DB
SELECT current_user AS certus_migration_role \gset
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE :"certus_database_name" FROM PUBLIC;
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'certus_runtime_user') AS certus_runtime_exists \gset
\if :certus_runtime_exists
\else
CREATE ROLE :"certus_runtime_user" WITH LOGIN NOINHERIT PASSWORD :'certus_runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\endif
SELECT (
  NOT role.rolcanlogin
  OR role.rolsuper
  OR role.rolcreatedb
  OR role.rolcreaterole
  OR role.rolreplication
  OR role.rolbypassrls
  OR EXISTS (SELECT 1 FROM pg_auth_members AS membership WHERE membership.member = role.oid OR membership.roleid = role.oid)
  OR EXISTS (SELECT 1 FROM pg_database WHERE datdba = role.oid)
  OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname <> 'information_schema' AND left(nspname, 3) <> 'pg_' AND nspowner = role.oid)
  OR EXISTS (
    SELECT 1 FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname <> 'information_schema' AND left(namespace.nspname, 3) <> 'pg_' AND relation.relowner = role.oid
  )
  OR EXISTS (
    SELECT 1 FROM pg_proc AS routine
    JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
    WHERE namespace.nspname <> 'information_schema' AND left(namespace.nspname, 3) <> 'pg_' AND routine.proowner = role.oid
  )
  OR EXISTS (
    SELECT 1 FROM pg_type AS type
    JOIN pg_namespace AS namespace ON namespace.oid = type.typnamespace
    WHERE namespace.nspname <> 'information_schema' AND left(namespace.nspname, 3) <> 'pg_' AND type.typowner = role.oid
  )
  OR EXISTS (
    SELECT 1 FROM pg_extension AS extension
    JOIN pg_namespace AS namespace ON namespace.oid = extension.extnamespace
    WHERE namespace.nspname <> 'information_schema' AND left(namespace.nspname, 3) <> 'pg_' AND extension.extowner = role.oid
  )
  OR EXISTS (SELECT 1 FROM pg_largeobject_metadata WHERE lomowner = role.oid)
  OR has_database_privilege(role.oid, current_database(), 'CREATE')
  OR has_database_privilege(role.oid, current_database(), 'TEMPORARY')
  OR EXISTS (
    SELECT 1 FROM pg_namespace AS namespace
    WHERE namespace.nspname <> 'information_schema'
      AND left(namespace.nspname, 3) <> 'pg_'
      AND has_schema_privilege(role.oid, namespace.oid, 'CREATE')
  )
) AS certus_runtime_is_unsafe
FROM pg_roles AS role
WHERE role.rolname = :'certus_runtime_user' \gset
\if :certus_runtime_is_unsafe
\echo Runtime database role has unsafe attributes, membership, ownership, or create privileges.
SELECT 1 / 0;
\endif
GRANT TEMPORARY ON DATABASE :"certus_database_name" TO :"certus_migration_role";
REVOKE CREATE, TEMPORARY ON DATABASE :"certus_database_name" FROM :"certus_runtime_user";
GRANT CONNECT ON DATABASE :"certus_database_name" TO :"certus_runtime_user";
REVOKE CREATE ON SCHEMA public FROM :"certus_runtime_user";
GRANT USAGE ON SCHEMA public TO :"certus_runtime_user";
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM :"certus_runtime_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"certus_runtime_user";
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM :"certus_runtime_user";
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO :"certus_runtime_user";
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM :"certus_runtime_user";
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"certus_runtime_user";
SELECT format('GRANT USAGE ON TYPE %I.%I TO %I', namespace.nspname, type.typname, :'certus_runtime_user')
FROM pg_type AS type
JOIN pg_namespace AS namespace ON namespace.oid = type.typnamespace
WHERE namespace.nspname = 'public'
  AND pg_has_role(current_user, type.typowner, 'USAGE')
\gexec
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" REVOKE ALL ON TABLES FROM :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" REVOKE ALL ON SEQUENCES FROM :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" IN SCHEMA public GRANT USAGE ON SEQUENCES TO :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" REVOKE EXECUTE ON FUNCTIONS FROM :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" REVOKE ALL ON TYPES FROM :"certus_runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"certus_migration_role" IN SCHEMA public GRANT USAGE ON TYPES TO :"certus_runtime_user";
REVOKE ALL PRIVILEGES ON TABLE public.schema_migrations FROM PUBLIC, :"certus_runtime_user";
COMMIT;
SQL
  printf "SELECT pg_advisory_unlock(hashtextextended('certus:schema-migrations:v1', 0));\n"
} | run_psql -q

echo "✅ All database migrations applied successfully!"
