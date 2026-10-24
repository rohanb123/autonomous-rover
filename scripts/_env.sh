# Sourced by the other scripts: loads config/.env if present.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/config/.env" ]]; then
  set -a; source "$ROOT/config/.env"; set +a
fi
: "${STDB_URL:=http://192.168.50.10:3000}"
: "${STDB_DB:=marsrover}"
export PYTHONPATH="$ROOT/shared/py:$ROOT/rover:$ROOT/gx10${PYTHONPATH:+:$PYTHONPATH}"
: "${STDB_SERVER:=local}"   # spacetime CLI server name (-s); "gx10" from a laptop, see docs/02 §2
