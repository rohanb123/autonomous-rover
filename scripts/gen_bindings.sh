#!/usr/bin/env bash
# Regenerate dashboard/src/module_bindings (never hand-edit them). docs/02 §2.
set -euo pipefail
source "$(dirname "$0")/_env.sh"

cd "$ROOT/spacetimedb"
spacetime generate --lang typescript -o ../dashboard/src/module_bindings -p .
