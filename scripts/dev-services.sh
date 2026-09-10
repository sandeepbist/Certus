#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if ! command -v bun >/dev/null 2>&1; then
  echo "❌ Bun is required. Install Bun, then run 'make setup'."
  exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
  echo "❌ The local Python environment is missing. Run 'make setup' first."
  exit 1
fi
if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "❌ .env is missing. Run 'make setup' first."
  exit 1
fi

declare -a SERVICE_PIDS=()
declare -a SERVICE_NAMES=()

start_service() {
  local service_name="$1"
  shift
  echo "  ▶ $service_name"
  "$@" &
  SERVICE_PIDS+=("$!")
  SERVICE_NAMES+=("$service_name")
}

stop_services() {
  local pid
  trap - EXIT INT TERM
  for pid in "${SERVICE_PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${SERVICE_PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

trap stop_services EXIT
trap 'exit 130' INT TERM

cd "$ROOT_DIR"
echo "🚀 Starting the complete Certus application stack..."
start_service "Web UI             http://localhost:3000" bun run dev:web
start_service "Gateway            http://localhost:4000" bun run dev:gateway
start_service "Ingestion          http://localhost:8001" \
  env PYTHONPATH="$ROOT_DIR/services/ingestion:$ROOT_DIR" \
  "$PYTHON_BIN" -m uvicorn app.main:app \
  --app-dir "$ROOT_DIR/services/ingestion" --host 0.0.0.0 --port 8001 \
  --reload --reload-dir "$ROOT_DIR/services/ingestion"
start_service "Orchestration      http://localhost:8002" \
  env PYTHONPATH="$ROOT_DIR/services/orchestration:$ROOT_DIR" \
  "$PYTHON_BIN" -m uvicorn app.main:app \
  --app-dir "$ROOT_DIR/services/orchestration" --host 0.0.0.0 --port 8002 \
  --reload --reload-dir "$ROOT_DIR/services/orchestration" \
  --reload-dir "$ROOT_DIR/services/shared"
start_service "MCP tools          http://localhost:8003" \
  env PYTHONPATH="$ROOT_DIR/services/mcp-tools:$ROOT_DIR" \
  "$PYTHON_BIN" -m uvicorn app.server:app \
  --app-dir "$ROOT_DIR/services/mcp-tools" --host 0.0.0.0 --port 8003 \
  --reload --reload-dir "$ROOT_DIR/services/mcp-tools"
start_service "Embedding worker" \
  env PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" services/embedding/app/worker.py

echo "✅ Application processes started. Infrastructure and the Temporal worker run in Docker."
echo "   Press Ctrl+C once to stop every local application process."

while true; do
  for index in "${!SERVICE_PIDS[@]}"; do
    pid="${SERVICE_PIDS[$index]}"
    if ! kill -0 "$pid" 2>/dev/null; then
      set +e
      wait "$pid"
      exit_code="$?"
      set -e
      echo "❌ ${SERVICE_NAMES[$index]} exited with status $exit_code; stopping the stack."
      exit "$exit_code"
    fi
  done
  sleep 1
done
