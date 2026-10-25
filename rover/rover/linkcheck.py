"""Pi <-> GX10 link check: every doc 00 section 3 port, the rover WS, clock skew, mapper health.

    python -m rover.linkcheck                                # from the Pi or the GX10
    python -m rover.linkcheck --only pi --pi 127.0.0.1       # laptop against main.py / fake_rover

Prints one line per check and exits 1 if any check FAILs. Side-effect free: the round-trip
probe is a cmd with an op that does not exist, which ws_server answers with `err E_BAD_ARGS`
before dispatch -- it never reaches the backend, never moves anything, and is safe mid-run.

That reply's `t` is stamped by the rover's clock, so the same probe measures clock offset
NTP-style: offset = t_rover - (t_sent + t_received) / 2, taken from the fastest round trip,
whose midpoint is the tightest bound. Doc 00 section 4 allows 50 ms.

websockets + httpx + pydantic only, like ws_server: this has to run on the GX10 and on a
laptop, neither of which has the Pi's hardware libraries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a file rather than -m, so bootstrap the paths
    _ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_ROOT / "shared" / "py"))
    sys.path.insert(0, str(_ROOT / "rover"))

import httpx  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

from marsrover_shared.schemas import Err, Telemetry, parse_rover_message  # noqa: E402

PI = "192.168.50.20"
GX10 = "192.168.50.10"
ROVER_WS_PORT = 8765
MAPPER_PORT = 8200


def port_table(
    ws_port: int = ROVER_WS_PORT, mapper_port: int = MAPPER_PORT
) -> dict[str, list[tuple[int, str, bool]]]:
    """Doc 00 section 3 as (port, label, required). Optional ports report `down`, not FAIL."""
    return {
        "pi": [(ws_port, "rover ws", True), (8080, "snapshot/stream", False)],
        "gx10": [
            (8000, "brain", True),
            (8100, "vision", True),
            (mapper_port, "mapper", True),
            (8300, "overhead", False),
            (3000, "spacetimedb", True),
        ],
    }


CONNECT_TIMEOUT_S = 1.0
TELEMETRY_WAIT_S = 1.0  # 5 Hz, so five frames' worth of slack
PROBES = 20
MAX_SKEW_S = 0.050  # doc 00 section 4
MAX_RTT_P95_S = 0.250  # half the 0.5 s ack budget
PROBE_OP = "__linkcheck__"


@dataclass(frozen=True)
class Result:
    name: str
    status: str  # "ok" | "FAIL" | "down" (optional and unreachable)
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"


def _why(e: Exception) -> str:
    """Windows reports a refused localhost port as a ~1 s timeout; say what that means."""
    if isinstance(e, TimeoutError | httpx.TimeoutException):
        return (
            f"no answer within {CONNECT_TIMEOUT_S:g} s (service not running, or host unreachable)"
        )
    return str(e) or type(e).__name__


# --- checks --------------------------------------------------------------------------------


async def check_port(host: str, port: int, label: str, required: bool) -> Result:
    name = f"tcp {host}:{port} ({label})"
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT_S)
    except (OSError, TimeoutError) as e:
        return Result(name, "FAIL" if required else "down", _why(e))
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return Result(name, "ok")


def _probe_frame(n: int) -> str:
    return json.dumps({"type": "cmd", "src": "brain", "cmd_id": f"lc_{n:06d}", "op": PROBE_OP})


async def check_rover_ws(
    url: str, probes: int = PROBES, clock: Callable[[], float] = time.time
) -> list[Result]:
    """Telemetry parses; round trip p50/p95; clock offset from the fastest probe."""
    try:
        async with connect(url, open_timeout=CONNECT_TIMEOUT_S, ping_interval=None) as ws:
            telemetry = await _first_telemetry(ws)
            samples = [await _probe(ws, n, clock) for n in range(probes)]
    except (OSError, TimeoutError) as e:
        return [Result(f"ws {url}", "FAIL", _why(e))]
    except ValueError as e:  # pydantic: the rover sent something off-contract
        return [Result(f"ws {url}", "FAIL", f"off-contract frame: {e}")]

    out = [
        Result(
            "rover telemetry",
            "ok",
            f"state={telemetry.state} estop={telemetry.estop} run={telemetry.run_id or '-'} "
            f"backlog={telemetry.uplink_backlog}",
        )
    ]
    rtts = sorted(rtt for rtt, _ in samples)
    p50 = statistics.median(rtts)
    p95 = rtts[min(len(rtts) - 1, round(0.95 * (len(rtts) - 1)))]
    out.append(
        Result(
            "ws round trip",
            "ok" if p95 <= MAX_RTT_P95_S else "FAIL",
            f"p50 {p50 * 1e3:.1f} ms, p95 {p95 * 1e3:.1f} ms over {len(rtts)} "
            f"(limit p95 {MAX_RTT_P95_S * 1e3:.0f} ms)",
        )
    )
    _, offset = min(samples)
    out.append(
        Result(
            "clock offset (rover - here)",
            "ok" if abs(offset) <= MAX_SKEW_S else "FAIL",
            f"{offset * 1e3:+.1f} ms (limit +/-{MAX_SKEW_S * 1e3:.0f} ms; fix: chronyc tracking)",
        )
    )
    return out


async def _first_telemetry(ws) -> Telemetry:
    async def wait() -> Telemetry:
        async for raw in ws:
            msg = parse_rover_message(raw)
            if isinstance(msg, Telemetry):
                return msg
        raise OSError("socket closed before any telemetry")

    return await asyncio.wait_for(wait(), TELEMETRY_WAIT_S)


async def _probe(ws, n: int, clock: Callable[[], float]) -> tuple[float, float]:
    """One bad-op round trip -> (rtt_s, offset_s). Telemetry and events in between are skipped."""
    cmd_id = f"lc_{n:06d}"
    t_sent = clock()
    await ws.send(_probe_frame(n))

    async def answer() -> Err:
        async for raw in ws:
            msg = parse_rover_message(raw)
            if isinstance(msg, Err) and msg.cmd_id == cmd_id:
                return msg
        raise OSError("socket closed mid-probe")

    err = await asyncio.wait_for(answer(), CONNECT_TIMEOUT_S)
    t_recv = clock()
    return t_recv - t_sent, err.t - (t_sent + t_recv) / 2


async def check_mapper(base: str) -> Result:
    name = f"mapper {base}/health"
    try:
        async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_S * 2) as client:
            r = await client.get(f"{base}/health")
            r.raise_for_status()
            body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return Result(name, "FAIL", _why(e))
    if body.get("ok") is not True:
        return Result(name, "FAIL", f"unexpected body {body}")
    # stdb false is the mapper's problem, not the link's: report it, don't fail on it.
    return Result(name, "ok", f"stdb={body.get('stdb')}")


# --- runner --------------------------------------------------------------------------------


async def run_checks(
    pi: str = PI,
    gx10: str = GX10,
    only: str = "all",
    ws_port: int = ROVER_WS_PORT,
    mapper_port: int = MAPPER_PORT,
    clock: Callable[[], float] = time.time,
) -> list[Result]:
    hosts = {"pi": pi, "gx10": gx10}
    groups = ("pi", "gx10") if only == "all" else (only,)
    table = port_table(ws_port, mapper_port)
    ports = [
        check_port(hosts[g], port, label, required)
        for g in groups
        for port, label, required in table[g]
    ]
    results = list(await asyncio.gather(*ports))
    if "pi" in groups:
        results += await check_rover_ws(f"ws://{pi}:{ws_port}/rover", clock=clock)
    if "gx10" in groups:
        results.append(await check_mapper(f"http://{gx10}:{mapper_port}"))
    return results


def render(results: Sequence[Result]) -> str:
    width = max(len(r.name) for r in results)
    return "\n".join(f"{r.status:>4}  {r.name:<{width}}  {r.detail}" for r in results)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--pi", default=PI, help=f"rover address (default {PI})")
    p.add_argument("--gx10", default=GX10, help=f"GX10 address (default {GX10})")
    p.add_argument("--only", choices=("all", "pi", "gx10"), default="all")
    p.add_argument("--ws-port", type=int, default=ROVER_WS_PORT)
    p.add_argument("--mapper-port", type=int, default=MAPPER_PORT)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = asyncio.run(run_checks(args.pi, args.gx10, args.only, args.ws_port, args.mapper_port))
    print(render(results))  # noqa: T201 -- a CLI report, not a log line
    return 1 if any(r.failed for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
