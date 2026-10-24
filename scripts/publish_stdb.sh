#!/usr/bin/env bash
# Publish the "marsrover" module and register writers (docs/02 §2, §7).
set -euo pipefail
source "$(dirname "$0")/_env.sh"

cd "$ROOT/spacetimedb"
npm install --silent
spacetime publish "$STDB_DB" -s "$STDB_SERVER" -p . -y

call() {  # call <reducer> <json-args-array>
  curl -fsS -X POST "$STDB_URL/v1/database/$STDB_DB/call/$1" \
    -H "Authorization: Bearer $STDB_TOKEN_BRAIN" -H "Content-Type: application/json" -d "$2"
}

if [[ -z "${STDB_TOKEN_BRAIN:-}" ]]; then
  echo "STDB_TOKEN_BRAIN not set in config/.env; skipping claim_writer/add_writer" >&2
  exit 0
fi
call claim_writer '["brain"]' || echo "claim_writer failed (already claimed?)" >&2
if [[ -n "${STDB_IDENTITY_MAPPER:-}" ]]; then
  call add_writer "[\"$STDB_IDENTITY_MAPPER\",\"mapper\"]" || echo "add_writer failed" >&2
fi
