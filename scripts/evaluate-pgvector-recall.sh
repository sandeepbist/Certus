#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_runner="python3"
if [[ -x "$repository_root/.venv/bin/python" ]]; then
  python_runner="$repository_root/.venv/bin/python"
fi

cd "$repository_root"
exec "$python_runner" scripts/evaluate-pgvector-recall.py "$@"
