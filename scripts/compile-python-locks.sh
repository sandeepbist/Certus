#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to compile Python dependency locks" >&2
  exit 1
fi

required_uv_version="0.12.9"
read -r _ installed_uv_version _ < <(uv --version)
if [[ "$installed_uv_version" != "$required_uv_version" ]]; then
  echo "uv $required_uv_version is required; found $installed_uv_version" >&2
  exit 1
fi

requirement_inputs=(
  requirements-dev.txt
  services/embedding/requirements.txt
  services/ingestion/requirements.txt
  services/mcp-tools/requirements.txt
  services/orchestration/requirements.txt
  services/workflows/requirements.txt
)

for requirement_input in "${requirement_inputs[@]}"; do
  lock_file="${requirement_input%.txt}.lock"
  uv pip compile \
    --quiet \
    --python-version 3.12 \
    --universal \
    --generate-hashes \
    --output-file "$lock_file" \
    "$requirement_input"
done
