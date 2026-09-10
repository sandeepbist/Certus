#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_runner="python3"
if [[ -x "$repository_root/.venv/bin/python" ]]; then
  python_runner="$repository_root/.venv/bin/python"
fi

cd "$repository_root"
"$python_runner" -m compileall -q services tests/python
"$python_runner" -m unittest discover -s tests/python -p 'test_*.py'
