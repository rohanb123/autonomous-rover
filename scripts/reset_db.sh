#!/usr/bin/env bash
# DEV ONLY: wipe all data and republish, then re-register writers (docs/02 §2, §7.5).
set -euo pipefail
source "$(dirname "$0")/_env.sh"

read -r -p "Delete ALL data in '$STDB_DB' on '$STDB_SERVER'? [y/N] " ans
[[ "$ans" == "y" ]] || exit 1

cd "$ROOT/spacetimedb"
spacetime publish "$STDB_DB" -s "$STDB_SERVER" -p . --delete-data=always -y
"$ROOT/scripts/publish_stdb.sh"
