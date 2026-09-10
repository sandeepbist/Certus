#!/usr/bin/env bash

# Read the last literal KEY=value assignment from an env file without executing
# shell syntax, expanding variables, or interpreting command substitutions.
certus_read_env_value() {
  local env_file="$1"
  local variable_name="$2"
  local candidate=""
  local value=""

  if [[ ! "$variable_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Invalid environment variable name: $variable_name" >&2
    return 2
  fi
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi

  while IFS= read -r candidate || [[ -n "$candidate" ]]; do
    candidate="${candidate%$'\r'}"
    if [[ "$candidate" =~ ^[[:space:]]*${variable_name}[[:space:]]*=(.*)$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
  done < "$env_file"

  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if (( ${#value} >= 2 )); then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi

  printf '%s' "$value"
}

# Preserve a non-empty process value; otherwise load the literal env-file value
# and finally use the supplied local-development fallback.
certus_export_env_default() {
  local variable_name="$1"
  local fallback_value="$2"
  local env_file="$3"
  local file_value=""

  if [[ -n "${!variable_name-}" ]]; then
    export "$variable_name"
    return
  fi

  file_value="$(certus_read_env_value "$env_file" "$variable_name" || true)"
  printf -v "$variable_name" '%s' "${file_value:-$fallback_value}"
  export "$variable_name"
}
