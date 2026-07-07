#!/usr/bin/env bash
# Verify the server .env defines every key listed in .env.prod.example.
# Catches "new env var added in a release but never set on the server".
#
# Usage: scripts/check_env.sh [path-to-env] [path-to-template]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-${ROOT_DIR}/.env}"
TEMPLATE="${2:-${ROOT_DIR}/.env.prod.example}"

[[ -f "$ENV_FILE" ]] || { echo "ERROR: env file not found: $ENV_FILE" >&2; exit 1; }
[[ -f "$TEMPLATE" ]] || { echo "ERROR: template not found: $TEMPLATE" >&2; exit 1; }

# Extract REQUIRED key names (strip comments/blank lines, take text before first
# '='). Lines tagged with a trailing "# optional" are excluded — they may be unset
# on the server without failing the deploy (e.g. Flower basic auth, which is also
# gated by Cloudflare Access).
keys_of() {
  grep -vE '^[[:space:]]*(#|$)' "$1" | grep -viE '#[[:space:]]*optional' | sed -E 's/^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\1/' | sort -u
}

missing=()
while IFS= read -r key; do
  [[ -z "$key" ]] && continue
  if ! grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
    missing+=("$key")
  fi
done < <(keys_of "$TEMPLATE")

if (( ${#missing[@]} > 0 )); then
  echo "ERROR: ${ENV_FILE} is missing required keys:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi

echo "OK: ${ENV_FILE} defines all keys from $(basename "$TEMPLATE")."
