"""Send rover ops by hand (doc 00 section 6.2): bring-up and bench tests without the brain.

    python rover/rover/sendcmd.py --pi 192.168.50.20 start_run run_id=r_20261024_120000 \\
        -- station station_id=s_0001 n_views=1
    python rover/rover/sendcmd.py turn deg=90 speed=0.5
    python rover/rover/sendcmd.py --pi 127.0.0.1 snapshot

Ops run in order, separated by `--`; arguments are key=value, with numbers and true/false
typed. Each op prints its `done` result (or `err`) as JSON; the first `err` stops the
sequence and the exit code is 1. A heartbeat goes out every 0.25 s the whole time, exactly
like the brain, so a `turn` is not cut short by the 1.0 s watchdog.

Safe alongside the brain: the rover serves several clients, and replies go only to the
socket that sent the command. websockets + pydantic only, like linkcheck.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file rather than -m, so bootstrap the paths
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_ROOT / "shared" / "py"))
    sys.path.insert(0, str(_ROOT / "rover"))

from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import WebSocketException  # noqa: E402

from marsrover_shared.schemas import Ack, Cmd, Done, Err, parse_rover_message  # noqa: E402
from rover import ws_server  # noqa: E402

PI = "192.168.50.20"
HEARTBEAT_S = 0.25  # doc 00 section 4
ACK_WAIT_S = 2.0  # the contract budget is 0.5 s; be lenient on a bench, but report it
DEFAULT_DONE_S = 30.0

Step = tuple[str, dict[str, Any]]


def typed(value: str) -> Any:
    """'1' -> 1, '0.5' -> 0.5, 'true' -> True; ids like 'r_2026…' and 's_0001' stay strings."""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            pass
    return value


def parse_steps(tokens: Sequence[str]) -> list[Step]:
    steps: list[Step] = []
    current: list[str] = []
    for tok in [*tokens, "--"]:
        if tok != "--":
            current.append(tok)
            continue
        if not current:
            continue
        op, *pairs = current
        args: dict[str, Any] = {}
        for pair in pairs:
            key, sep, value = pair.partition("=")
            if not sep or not key:
                raise SystemExit(f"{op}: expected key=value, got {pair!r}")
            args[key] = typed(value)
        steps.append((op, args))
        current = []
    if not steps:
        raise SystemExit("no op given (e.g. `snapshot`, or `start_run run_id=r_...`)")
    return steps


def split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Options come first; the first token that isn't an option starts the ops.

    argparse can't be trusted with `--` as a separator between ops (it swallows the first
    one), so the split is done here.
    """
    opts: list[str] = []
    i = 0
    while i < len(argv) and argv[i].startswith("--") and argv[i] != "--":
        opts.append(argv[i])
        if "=" not in argv[i] and argv[i] not in ("--help",):
            if i + 1 < len(argv):
                opts.append(argv[i + 1])
            i += 1
        i += 1
    return opts, list(argv[i:])


async def _heartbeat(ws) -> None:
    n = 0
    while True:
        n += 1
        with contextlib.suppress(Exception):
            await ws.send(Cmd(cmd_id=f"hb_{n:06d}", op="heartbeat").model_dump_json())
        await asyncio.sleep(HEARTBEAT_S)


async def _reply(ws, cmd_id: str, timeout: float) -> Ack | Done | Err:
    async def wait() -> Ack | Done | Err:
        async for raw in ws:
            msg = parse_rover_message(raw)
            if isinstance(msg, Ack | Done | Err) and msg.cmd_id == cmd_id:
                return msg
        raise ConnectionError("rover closed the socket")

    return await asyncio.wait_for(wait(), timeout)


async def run_steps(url: str, steps: Sequence[Step], out=sys.stdout) -> int:
    """Send each step; print its result. 0 if every op answered `done`, else 1."""
    async with connect(url, open_timeout=3, ping_interval=None, max_size=None) as ws:
        beat = asyncio.create_task(_heartbeat(ws))
        try:
            for n, (op, args) in enumerate(steps, start=1):
                cmd = Cmd(cmd_id=f"sc_{n:06d}", op=op, args=args)  # validates args (pydantic)
                print(f"> {op} {json.dumps(cmd.args)}", file=out)
                await ws.send(cmd.model_dump_json())
                if op == "heartbeat":
                    continue  # never answered (doc 00 section 6.2)
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                first = await _reply(ws, cmd.cmd_id, ACK_WAIT_S)
                if isinstance(first, Ack):
                    ack_ms = (loop.time() - t0) * 1e3
                    late = "" if ack_ms <= ws_server.ACK_BUDGET_S * 1e3 else "  (over budget!)"
                    print(f"  ack {ack_ms:.0f} ms{late}", file=out)
                    limit = ws_server.OP_TIMEOUT_S.get(op, DEFAULT_DONE_S) + 5.0
                    final = await _reply(ws, cmd.cmd_id, limit)
                else:
                    final = first  # bad args are answered with err and no ack
                if isinstance(final, Err):
                    print(f"  err {final.code}: {final.msg}", file=out)
                    return 1
                print(json.dumps(final.result, indent=2), file=out)
                print(f"  done in {loop.time() - t0:.1f} s{summary(op, final.result)}", file=out)
        finally:
            beat.cancel()
    return 0


def summary(op: str, result: dict[str, Any]) -> str:
    """One line a human can check without reading the JSON."""
    if op != "station":
        return ""
    mapper = result.get("mapper", {})
    return (
        f"; {len(result.get('keyframes', []))} keyframe(s), "
        f"{len(result.get('proposals', []))} proposal(s), "
        f"mapper.ok={mapper.get('ok')} stored={mapper.get('stored') or '-'}"
    )


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    opt_tokens, step_tokens = split_argv(raw)
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--pi", default=PI, help=f"rover address (default {PI})")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args(opt_tokens)
    steps = parse_steps(step_tokens)
    url = f"ws://{args.pi}:{args.port}{ws_server.PATH}"
    try:
        return asyncio.run(run_steps(url, steps))
    except (OSError, TimeoutError, ConnectionError, WebSocketException) as e:
        print(f"! {url}: {e or type(e).__name__}", file=sys.stderr)
        return 1
    except ValueError as e:  # pydantic: bad op name or args, before anything was sent
        print(f"! invalid command: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
