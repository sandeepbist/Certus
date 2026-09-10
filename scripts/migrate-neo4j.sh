#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
# shellcheck source=scripts/lib/env-file.sh
source "$ROOT_DIR/scripts/lib/env-file.sh"

certus_export_env_default NEO4J_USER neo4j "$ROOT_DIR/.env"
certus_export_env_default NEO4J_PASSWORD nexus_neo4j_dev "$ROOT_DIR/.env"

if ! docker inspect nexus-neo4j >/dev/null 2>&1; then
  echo "❌ Neo4j container 'nexus-neo4j' is not available."
  exit 1
fi

echo "🕸️  Applying Neo4j constraints and indexes..."
docker exec -i nexus-neo4j cypher-shell \
  -u "$NEO4J_USER" \
  -p "$NEO4J_PASSWORD" \
  --non-interactive \
  < infra/neo4j/schema.cypher
echo "✅ Neo4j schema is current."
