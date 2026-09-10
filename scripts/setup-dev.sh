#!/usr/bin/env bash
# ============================================================
# Certus — complete local development setup
# ============================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
# shellcheck source=scripts/lib/env-file.sh
source "$ROOT_DIR/scripts/lib/env-file.sh"

echo "============================================================"
echo "🚀 Initializing the Certus development environment"
echo "============================================================"

# 1. Prepare environment variables
if [ ! -f .env ]; then
  echo "📄 Creating .env from .env.example..."
  cp .env.example .env
fi
chmod 600 .env

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
.venv/bin/python -m pip install -r requirements-dev.txt

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
