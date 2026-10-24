"""replay_run: rebuild a run in SpacetimeDB from its journal (data/runs/<run>/stdb_calls.jsonl).

Every reducer call the brain and mapper make is journaled before it reaches the DB
(marsrover_shared.journal), so if STDB dies mid-run, or you reset it, the run can be re-sent:

    python mocks/replay_run.py r_20261024_071839                # into config/.env's STDB, instant
    python mocks/replay_run.py r_20261024_071839 --speed 1      # at the recorded pace
    python mocks/replay_run.py r_20261024_071839 --as r_copy    # alongside the original
    python mocks/replay_run.py r_20261024_071839 --dry-run      # just summarise the journal

Calls keep their recorded order and their own tUnix values, so replay looks like the original.
--as renames the run id (and g_<run>_ tile ids) but leaves file URLs pointing at the original
run's files on the mapper. rover_status is skipped unless --with-status (it's the live row).
Writes with STDB_TOKEN_BRAIN, which is a registered writer (docs/02 §7).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "py"))  # works without an install (macOS .pth issue)

from marsrover_shared import journal  # noqa: E402
from marsrover_shared.stdb_http import StdbClient, StdbError  # noqa: E402

SKIP_BY_DEFAULT = {"upsert_rover_status"}


def rename(call: dict, old: str, new: str) -> dict:
    """Swap the run id: args equal to it, and g_<old>_ tile ids. URLs are left alone."""
    args = []
    for a in call["args"]:
        if a == old:
            a = new
        elif isinstance(a, str) and a.startswith(f"g_{old}_"):
            a = f"g_{new}_" + a[len(old) + 3 :]
        args.append(a)
    return {**call, "args": args}


def plan(
    calls: Iterable[dict], rename_to: str | None, run_id: str, with_status: bool
) -> list[dict]:
    out = []
    for c in calls:
        if c["reducer"] in SKIP_BY_DEFAULT and not with_status:
            continue
        out.append(rename(c, run_id, rename_to) if rename_to else c)
    return out


def replay(client, calls: list[dict], speed: float = 0.0, log=print) -> Counter:
    """Send calls in order. speed 0 = as fast as possible; N = recorded pace / N."""
    sent: Counter = Counter()
    failed: Counter = Counter()
    prev_t = calls[0]["t"] if calls else 0.0
    for c in calls:
        if speed > 0:
            time.sleep(max(0.0, c["t"] - prev_t) / speed)
        prev_t = c["t"]
        try:
            client.call(c["reducer"], *c["args"])
            sent[c["reducer"]] += 1
        except StdbError as e:
            if c["reducer"] == "start_run":
                raise SystemExit(
                    f"start_run refused ({e.body[:120]}): the run already exists in this DB. "
                    "Replay into a fresh DB (scripts/reset_db.sh) or use --as <new_run_id>."
                ) from e
            failed[c["reducer"]] += 1
            log(f"  {c['reducer']} failed: {e.body[:120]}")
    sent.update({f"FAILED {k}": v for k, v in failed.items()})
    return sent


def load_env() -> None:
    env = ROOT / "config" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_id")
    ap.add_argument("--as", dest="rename_to", help="replay under this run id instead")
    ap.add_argument("--speed", type=float, default=0.0, help="0 = instant (default), 1 = real time")
    ap.add_argument("--with-status", action="store_true", help="also replay rover_status")
    ap.add_argument("--dry-run", action="store_true", help="summarise the journal, send nothing")
    args = ap.parse_args()

    calls = journal.read(args.run_id)
    if not calls:
        sys.exit(f"no journal at {journal.path(args.run_id)}")
    todo = plan(calls, args.rename_to, args.run_id, args.with_status)
    span = calls[-1]["t"] - calls[0]["t"]
    target = args.rename_to or args.run_id
    print(f"{args.run_id}: {len(calls)} journaled calls over {span:.0f} s; ", end="")
    print(f"replaying {len(todo)} as {target}")
    if args.dry_run:
        for reducer, n in sorted(Counter(c["reducer"] for c in todo).items()):
            print(f"  {reducer:22} {n}")
        return
    load_env()
    if not os.environ.get("STDB_TOKEN_BRAIN"):
        sys.exit("STDB_TOKEN_BRAIN is not set: fill config/.env (docs/02 §7)")
    sent = replay(StdbClient.from_env("STDB_TOKEN_BRAIN"), todo, args.speed)
    for reducer, n in sorted(sent.items()):
        print(f"  {reducer:22} {n}")


if __name__ == "__main__":
    main()
