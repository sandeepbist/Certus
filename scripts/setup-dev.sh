#!/usr/bin/env bash
# ============================================================
# Certus — complete local development setup
# ============================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
# shellcheck source=scripts/lib/env-file.sh
source "$ROOT_DIR/scripts/lib/env-file.sh"

certus_assert_local_database_host() {
  local database_host="$1"
  database_host="$(printf '%s' "$database_host" | tr '[:upper:]' '[:lower:]')"
  database_host="${database_host#\[}"
  database_host="${database_host%\]}"
  case "$database_host" in
    localhost|127.0.0.1|::1) ;;
    *)
      echo "❌ make setup only supports local PostgreSQL hosts: localhost, 127.0.0.1, or ::1."
      echo "For managed databases, run scripts/migrate-db.sh as a controlled release step with explicit credentials."
      exit 1
      ;;
  esac
}

database_host_preflight="${DB_HOST-}"
if [[ -z "$database_host_preflight" ]]; then
  database_host_preflight="$(certus_read_env_value "$ROOT_DIR/.env" DB_HOST || true)"
fi
certus_assert_local_database_host "${database_host_preflight:-localhost}"

echo "============================================================"
echo "🚀 Initializing the Certus development environment"
echo "============================================================"

# 1. Prepare environment variables
if [ ! -f .env ]; then
  echo "📄 Creating .env from .env.example..."
  cp .env.example .env
fi
chmod 600 .env

# Local application services use a dedicated runtime role. Preserve custom
# database settings and only migrate the exact legacy local connection URL.
process_database_url="${DATABASE_URL-}"
file_database_url="$(certus_read_env_value "$ROOT_DIR/.env" DATABASE_URL || true)"
certus_export_env_default DB_HOST localhost "$ROOT_DIR/.env"
certus_export_env_default DB_PORT 5432 "$ROOT_DIR/.env"
certus_export_env_default POSTGRES_USER nexus "$ROOT_DIR/.env"
certus_export_env_default POSTGRES_PASSWORD nexus_dev_password "$ROOT_DIR/.env"
certus_export_env_default POSTGRES_DB nexus "$ROOT_DIR/.env"
certus_export_env_default CERTUS_RUNTIME_DB_USER certus_runtime "$ROOT_DIR/.env"
certus_export_env_default CERTUS_RUNTIME_DB_PASSWORD "" "$ROOT_DIR/.env"
certus_assert_local_database_host "$DB_HOST"

legacy_database_url="postgresql://nexus:nexus_dev_password@localhost:5432/nexus"
database_url_candidate="${process_database_url:-$file_database_url}"
database_url_needs_runtime_role=false
if [[ "$database_url_candidate" == "$legacy_database_url" ]] \
   && [[ "$DB_HOST" != localhost || "$DB_PORT" != 5432 || "$POSTGRES_USER" != nexus \
      || "$POSTGRES_PASSWORD" != nexus_dev_password || "$POSTGRES_DB" != nexus ]]; then
  echo "❌ The legacy local DATABASE_URL can be converted only with the original local database target."
  echo "Set DATABASE_URL explicitly to match CERTUS_RUNTIME_DB_USER, CERTUS_RUNTIME_DB_PASSWORD, DB_HOST, DB_PORT, and POSTGRES_DB."
  exit 1
fi
if [[ -z "$database_url_candidate" || "$database_url_candidate" == "$legacy_database_url" ]]; then
  database_url_needs_runtime_role=true
fi

if [[ ! "$DB_PORT" =~ ^[0-9]+$ ]] || (( DB_PORT < 1 || DB_PORT > 65535 )); then
  echo "❌ DB_PORT must be an integer between 1 and 65535."
  exit 1
fi
if [[ ! "$CERTUS_RUNTIME_DB_USER" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]]; then
  echo "❌ CERTUS_RUNTIME_DB_USER must be a PostgreSQL role name using letters, digits, and underscores."
  exit 1
fi
if [[ "$CERTUS_RUNTIME_DB_USER" == "$POSTGRES_USER" ]]; then
  echo "❌ Runtime and migration database users must be different roles."
  exit 1
fi
if [[ "$database_url_needs_runtime_role" != true && -z "$CERTUS_RUNTIME_DB_PASSWORD" ]]; then
  echo "❌ A custom DATABASE_URL requires CERTUS_RUNTIME_DB_PASSWORD."
  echo "Set CERTUS_RUNTIME_DB_PASSWORD and make DATABASE_URL match CERTUS_RUNTIME_DB_USER, DB_HOST, DB_PORT, and POSTGRES_DB."
  exit 1
fi

runtime_password_generated=false
if [[ -z "$CERTUS_RUNTIME_DB_PASSWORD" ]]; then
  if ! command -v openssl &> /dev/null; then
    echo "❌ CERTUS_RUNTIME_DB_PASSWORD is empty and openssl is unavailable."
    exit 1
  fi
  CERTUS_RUNTIME_DB_PASSWORD="$(openssl rand -hex 32)"
  export CERTUS_RUNTIME_DB_PASSWORD
  runtime_password_generated=true
fi

if [[ "$database_url_needs_runtime_role" == true ]]; then
  DATABASE_URL="$(DB_HOST="$DB_HOST" DB_PORT="$DB_PORT" POSTGRES_DB="$POSTGRES_DB" CERTUS_RUNTIME_DB_USER="$CERTUS_RUNTIME_DB_USER" CERTUS_RUNTIME_DB_PASSWORD="$CERTUS_RUNTIME_DB_PASSWORD" python3 - <<'PY'
import os
from urllib.parse import quote, urlunsplit

host = os.environ["DB_HOST"].strip("[]")
if ":" in host:
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
else
  DATABASE_URL="$database_url_candidate"
fi

if ! DATABASE_URL="$DATABASE_URL" DB_HOST="$DB_HOST" DB_PORT="$DB_PORT" POSTGRES_DB="$POSTGRES_DB" CERTUS_RUNTIME_DB_USER="$CERTUS_RUNTIME_DB_USER" CERTUS_RUNTIME_DB_PASSWORD="$CERTUS_RUNTIME_DB_PASSWORD" python3 - <<'PY'
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

try:
    database_url = urlsplit(os.environ["DATABASE_URL"])
    target_host = os.environ["DB_HOST"].strip("[]").lower()
    query = parse_qs(database_url.query, keep_blank_values=True)
    database_path = database_url.path[1:] if database_url.path.startswith("/") else ""
    valid = (
        database_url.scheme in {"postgres", "postgresql"}
        and database_url.hostname is not None
        and database_url.hostname.lower() == target_host
        and (database_url.port or 5432) == int(os.environ["DB_PORT"])
        and unquote(database_path) == os.environ["POSTGRES_DB"]
        and unquote(database_url.username or "") == os.environ["CERTUS_RUNTIME_DB_USER"]
        and unquote(database_url.password or "") == os.environ["CERTUS_RUNTIME_DB_PASSWORD"]
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
  echo "❌ DATABASE_URL conflicts with the runtime role or database target."
  echo "Set DATABASE_URL to use CERTUS_RUNTIME_DB_USER and CERTUS_RUNTIME_DB_PASSWORD at DB_HOST:DB_PORT/POSTGRES_DB."
  echo "For managed databases, configure both migration and runtime credentials explicitly before running scripts/migrate-db.sh."
  exit 1
fi

if [[ "$database_url_candidate" == "$legacy_database_url" ]]; then
  echo "⚠️ Converting the exact legacy local DATABASE_URL to the dedicated runtime database role."
fi

export DATABASE_URL

certus_write_env_value() {
  local variable_name="$1"
  local variable_value="$2"

  CERTUS_ENV_WRITE_VALUE="$variable_value" python3 - "$ROOT_DIR/.env" "$variable_name" <<'PY'
import os
import re
import stat
import sys
import tempfile

env_path, variable_name = sys.argv[1:]
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable_name):
    raise SystemExit("invalid environment variable name")

env_mode = stat.S_IMODE(os.stat(env_path).st_mode)
with open(env_path, "r", encoding="utf-8", newline="") as env_file:
    lines = env_file.readlines()
assignment = re.compile(rf"^[ \t]*{re.escape(variable_name)}[ \t]*=")
matching_lines = [index for index, line in enumerate(lines) if assignment.match(line)]
replacement = f"{variable_name}={os.environ['CERTUS_ENV_WRITE_VALUE']}\n"
if matching_lines:
    lines[matching_lines[-1]] = replacement
else:
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.append(replacement)

file_descriptor, temporary_path = tempfile.mkstemp(dir=os.path.dirname(env_path), prefix=".env.")
try:
    with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as env_file:
        env_file.writelines(lines)
    os.chmod(temporary_path, env_mode)
    os.replace(temporary_path, env_path)
except BaseException:
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
    raise
PY
}

if [[ "$runtime_password_generated" == true ]]; then
  certus_write_env_value CERTUS_RUNTIME_DB_PASSWORD "$CERTUS_RUNTIME_DB_PASSWORD"
fi
file_runtime_password="$(certus_read_env_value "$ROOT_DIR/.env" CERTUS_RUNTIME_DB_PASSWORD || true)"
if [[ ( -z "$process_database_url" || "$process_database_url" == "$legacy_database_url" ) \
   && ( -z "$file_database_url" || "$file_database_url" == "$legacy_database_url" ) \
   && "$CERTUS_RUNTIME_DB_PASSWORD" == "$file_runtime_password" ]]; then
  certus_write_env_value DATABASE_URL "$DATABASE_URL"
fi

ensure_env_secret() {
  local variable_name="$1"

  if grep -Eq "^${variable_name}=.{32,}$" .env; then
    return
  fi
  if ! command -v openssl &> /dev/null; then
    echo "❌ $variable_name is missing and openssl is unavailable."
    exit 1
  fi

  local generated_value
  generated_value="$(openssl rand -hex 32)"
  if grep -q "^${variable_name}=" .env; then
    sed -i "s|^${variable_name}=.*$|${variable_name}=${generated_value}|" .env
  else
    printf '\n%s=%s\n' "$variable_name" "$generated_value" >> .env
  fi
  echo "🔐 Generated $variable_name for local development."
}

ensure_env_secret BETTER_AUTH_SECRET
ensure_env_secret INTERNAL_SERVICE_TOKEN
ensure_env_secret OBJECT_STORAGE_ACCESS_KEY
ensure_env_secret OBJECT_STORAGE_SECRET_KEY

# Docker Compose reads .env automatically. Export only the values used by the
# migration scripts, treating every env-file value as literal data.
certus_export_env_default POSTGRES_USER nexus "$ROOT_DIR/.env"
certus_export_env_default POSTGRES_PASSWORD nexus_dev_password "$ROOT_DIR/.env"
certus_export_env_default POSTGRES_DB nexus "$ROOT_DIR/.env"
certus_export_env_default NEO4J_USER neo4j "$ROOT_DIR/.env"
certus_export_env_default NEO4J_PASSWORD nexus_neo4j_dev "$ROOT_DIR/.env"

# 2. Install locked JavaScript dependencies.
if ! command -v bun &> /dev/null; then
  echo "❌ Bun is required. Install Bun and rerun setup."
  exit 1
fi
echo "📦 Installing locked JavaScript dependencies..."
bun install --frozen-lockfile

# 3. Create the local Python environment and install every service dependency.
PYTHON_COMMAND="${PYTHON_COMMAND:-python3}"
if ! command -v "$PYTHON_COMMAND" &> /dev/null; then
  echo "❌ Python 3.12 or newer is required."
  exit 1
fi
if ! "$PYTHON_COMMAND" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "❌ Python 3.12 or newer is required."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "🐍 Creating .venv..."
  "$PYTHON_COMMAND" -m venv .venv
fi
echo "📦 Installing Python service dependencies into .venv..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock

# 4. Start local infrastructure. Application services run as local processes so
# reload behavior is fast and their logs remain visible in one terminal.
if command -v docker &> /dev/null; then
  echo "🐳 Starting infrastructure containers (PostgreSQL+pgvector, object storage, Neo4j, Redis, Temporal)..."
  docker compose up -d postgres object-storage neo4j redis temporal
  
  echo "⏳ Waiting for PostgreSQL to become ready..."
  postgres_ready=false
  for i in {1..30}; do
    if docker exec nexus-postgres pg_isready \
      -U "${POSTGRES_USER:-nexus}" -d "${POSTGRES_DB:-nexus}" &> /dev/null; then
      echo "  ✅ PostgreSQL is ready!"
      postgres_ready=true
      break
    fi
    sleep 1
  done
  if [ "$postgres_ready" != true ]; then
    echo "❌ PostgreSQL did not become ready."
    exit 1
  fi

  echo "⏳ Waiting for object storage, Redis, Neo4j, and Temporal..."
  for port in 9000 6379 7687 7233; do
    ready=false
    for i in {1..60}; do
      if bash -c "</dev/tcp/127.0.0.1/$port" 2>/dev/null; then
        ready=true
        break
      fi
      sleep 1
    done
    if [ "$ready" != true ]; then
      echo "❌ Local dependency on port $port did not become ready."
      exit 1
    fi
  done

  # 5. Run schema migrations before starting the workflow worker.
  echo "🐘 Applying PostgreSQL schema migrations..."
  bash scripts/migrate-db.sh

  echo "🕸️ Applying Neo4j constraints and indexes..."
  bash scripts/migrate-neo4j.sh

  echo "⚙️ Starting the Temporal workflow worker and outbox dispatchers..."
  docker compose up -d --build workflows
else
  echo "❌ Docker with Compose is required for local infrastructure."
  exit 1
fi

echo "============================================================"
echo "🎉 Certus local setup completed successfully!"
echo "============================================================"
echo "  • Web Application:     http://localhost:3000"
echo "  • Fastify Gateway:     http://localhost:4000"
echo "  • Ingestion API:       http://localhost:8001"
echo "  • Orchestration API:   http://localhost:8002"
echo "  • MCP Tool Server:     http://localhost:8003"
echo "  • PostgreSQL:          localhost:5432 (pgvector active)"
echo "  • Original Storage:    http://localhost:9000 (private S3 API)"
echo "  • Storage Console:     http://localhost:9001"
echo "  • Neo4j Browser:       http://localhost:7474"
echo "  • Redis Cache/Streams: localhost:6379"
echo "  • Temporal UI:         http://localhost:8233"
echo "============================================================"
echo "To start development servers, run:"
echo "  make dev  (or 'bun run dev') — starts every application service"
echo "============================================================"
