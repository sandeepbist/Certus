#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

remove_tree() {
  local target="$1"
  if [ -d "$target" ]; then
    find "$target" -depth -delete
    echo "  ✓ Removed ${target#"$ROOT_DIR"/}"
  fi
}

remove_tree "$ROOT_DIR/services/web/.next"
remove_tree "$ROOT_DIR/services/gateway/dist"
remove_tree "$ROOT_DIR/.playwright-cli"
remove_tree "$ROOT_DIR/output"
remove_tree "$ROOT_DIR/coverage"
remove_tree "$ROOT_DIR/.pytest_cache"

find "$ROOT_DIR/services" "$ROOT_DIR/tests" "$ROOT_DIR/scripts" "$ROOT_DIR/evals" -type f \
  \( -name '*.pyc' -o -name '*.pyo' -o -name '*.tsbuildinfo' \) -delete
find "$ROOT_DIR/services" "$ROOT_DIR/tests" "$ROOT_DIR/scripts" "$ROOT_DIR/evals" -depth -type d \
  -name '__pycache__' -empty -delete

echo "🧹 Generated build and test artifacts are clean. Dependency environments were preserved."
