"""rover.linkcheck against fake_rover in-process (the Pi's own ws_server) and a stub mapper."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mocks"))

import fake_rover  # noqa: E402

from rover import linkcheck  # noqa: E402
from rover.linkcheck import Result  # noqa: E402


def run(coro, timeout: float = 15.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


@contextlib.asynccontextmanager
async def rover():
    sim = fake_rover.Sim(fake_rover.Options(speed_scale=20.0, no_upload=True))
    async with fake_rover.serve_rover(sim, "127.0.0.1", 0) as server:
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            sim.close()


@contextlib.contextmanager
def stub_mapper(body: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
            payload = json.dumps(body).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def free_port() -> int:
    """A port with nothing listening on it (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def by_name(results: list[Result], prefix: str) -> Result:
    return next(r for r in results if r.name.startswith(prefix))


# --- the rover side ------------------------------------------------------------------------


def test_rover_ws_passes_against_the_real_protocol_layer():
    async def go():
        async with rover() as port:
            return await linkcheck.check_rover_ws(f"ws://127.0.0.1:{port}/rover", probes=5)

    results = run(go())
    assert [r.status for r in results] == ["ok", "ok", "ok"], linkcheck.render(results)
    assert "state=IDLE" in by_name(results, "rover telemetry").detail


def test_the_probe_has_no_side_effects():
    """An unknown op is refused before dispatch: no state change, no run, no op slot taken."""

    async def go():
        sim = fake_rover.Sim(fake_rover.Options(speed_scale=20.0, no_upload=True))
        async with fake_rover.serve_rover(sim, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            before = sim.telemetry()
            await linkcheck.check_rover_ws(f"ws://127.0.0.1:{port}/rover", probes=10)
            after = sim.telemetry()
        sim.close()
        return before, after

    before, after = run(go())
    assert (after.state, after.run_id, after.pose) == (before.state, before.run_id, before.pose)


def test_clock_offset_beyond_50ms_fails():
    """Here runs 200 ms ahead of the rover, so rover - here reads about -200 ms."""

    async def go():
        async with rover() as port:
            return await linkcheck.check_rover_ws(
                f"ws://127.0.0.1:{port}/rover", probes=3, clock=lambda: time.time() + 0.2
            )

    skew = by_name(run(go()), "clock offset")
    assert skew.failed
    assert skew.detail.startswith("-2")  # "-200.x ms"


def test_unreachable_rover_is_a_failure_not_a_crash():
    results = run(linkcheck.check_rover_ws(f"ws://127.0.0.1:{free_port()}/rover"))
    assert len(results) == 1 and results[0].failed


# --- ports and the mapper ------------------------------------------------------------------


def test_down_optional_port_does_not_fail_but_down_required_port_does():
    port = free_port()
    optional = run(linkcheck.check_port("127.0.0.1", port, "x", required=False))
    required = run(linkcheck.check_port("127.0.0.1", port, "x", required=True))
    assert optional.status == "down" and not optional.failed
    assert required.failed


def test_mapper_health_ok_and_reports_stdb():
    with stub_mapper({"ok": True, "stdb": False}) as port:
        result = run(linkcheck.check_mapper(f"http://127.0.0.1:{port}"))
    assert result.status == "ok" and result.detail == "stdb=False"


def test_mapper_health_not_ok_fails():
    with stub_mapper({"ok": False}) as port:
        assert run(linkcheck.check_mapper(f"http://127.0.0.1:{port}")).failed


# --- end to end ----------------------------------------------------------------------------


def test_only_pi_exits_zero_with_the_snapshot_port_down():
    """The laptop case: rover up, :8080 not served by fake_rover -> `down`, still exit 0."""

    async def go():
        async with rover() as port:
            return await linkcheck.run_checks(pi="127.0.0.1", only="pi", ws_port=port)

    results = run(go())
    assert not any(r.failed for r in results), linkcheck.render(results)
    names = [r.name for r in results]
    assert any("snapshot" in n for n in names)
    assert not any("mapper" in n for n in names)  # gx10 group skipped


def test_main_exit_code_is_1_when_anything_fails(capsys):
    port = free_port()
    code = linkcheck.main(["--only", "pi", "--pi", "127.0.0.1", "--ws-port", str(port)])
    assert code == 1
    assert "FAIL" in capsys.readouterr().out
