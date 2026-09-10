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

database_host="$DB_HOST"
database_port="$DB_PORT"
database_user="$POSTGRES_USER"
database_name="$POSTGRES_DB"
database_password="$POSTGRES_PASSWORD"
migration_lock_timeout_seconds="$MIGRATION_LOCK_TIMEOUT_SECONDS"

if [[ ! "$migration_lock_timeout_seconds" =~ ^[0-9]+$ ]] \
   || (( migration_lock_timeout_seconds < 1 || migration_lock_timeout_seconds > 3600 )); then
  echo "❌ MIGRATION_LOCK_TIMEOUT_SECONDS must be an integer between 1 and 3600."
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
    docker exec -i nexus-postgres psql -U "$database_user" -d "$database_name" -v ON_ERROR_STOP=1 "$@"
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
  printf '%s\n' 'CREATE TABLE IF NOT EXISTS schema_migrations ('
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
    printf "    IF EXISTS (SELECT 1 FROM schema_migrations WHERE filename = '%s' AND checksum <> '%s') THEN\n" \
      "$migration_name" "$migration_checksum"
    printf "        RAISE EXCEPTION 'Applied migration was modified: %s';\n" "$migration_name"
    printf '%s\n' '    END IF;'
    printf '%s\n' 'END'
    printf '%s\n' '$certus_checksum$;'
    printf "SELECT NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = '%s') AS certus_should_apply \\gset\n" \
      "$migration_name"
    printf '%s\n' '\if :certus_should_apply'
    printf '\\echo %s\n' "  ▶ Applying $migration_name..."
    printf '%s\n' 'BEGIN;'
    sed -e '$a\' "$migration_file"
    printf "INSERT INTO schema_migrations (filename, checksum) VALUES ('%s', '%s');\n" \
      "$migration_name" "$migration_checksum"
    printf '%s\n' 'COMMIT;'
    printf '%s\n' '\else'
    printf '\\echo %s\n' "  ✓ Already applied $migration_name"
    printf '%s\n' '\endif'
  done

  printf "SELECT pg_advisory_unlock(hashtextextended('certus:schema-migrations:v1', 0));\n"
} | run_psql -q

echo "✅ All database migrations applied successfully!"
