"""rover.sendcmd: argument parsing, and a real op sequence against fake_rover in-process."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mocks"))

import fake_rover  # noqa: E402

from rover import sendcmd  # noqa: E402

RUN = "r_20261024_120000"


def test_values_are_typed_but_ids_stay_strings():
    steps = sendcmd.parse_steps(
        ["start_run", f"run_id={RUN}", "--", "turn", "deg=-90", "speed=0.5", "--", "stop"]
    )
    assert steps == [
        ("start_run", {"run_id": RUN}),
        ("turn", {"deg": -90, "speed": 0.5}),
        ("stop", {}),
    ]


def test_options_split_from_ops_even_with_double_dash_separators():
    opts, ops = sendcmd.split_argv(["--pi", "1.2.3.4", "start_run", "run_id=x", "--", "stop"])
    assert opts == ["--pi", "1.2.3.4"] and ops == ["start_run", "run_id=x", "--", "stop"]


def test_malformed_pair_is_rejected():
    with pytest.raises(SystemExit):
        sendcmd.parse_steps(["turn", "deg"])


@contextlib.asynccontextmanager
async def rover():
    sim = fake_rover.Sim(fake_rover.Options(speed_scale=20.0, no_upload=True))
    async with fake_rover.serve_rover(sim, "127.0.0.1", 0) as server:
        try:
            yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/rover"
        finally:
            sim.close()


def run(coro, timeout: float = 20.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_start_run_then_a_one_view_station_answers_done():
    out = io.StringIO()

    async def go():
        async with rover() as url:
            steps = sendcmd.parse_steps(
                ["start_run", f"run_id={RUN}", "--", "station", "station_id=s_0001", "n_views=1"]
            )
            return await sendcmd.run_steps(url, steps, out)

    assert run(go()) == 0
    text = out.getvalue()
    assert "> station" in text and "1 keyframe(s)" in text and "mapper.ok=True" in text


def test_an_err_stops_the_sequence_with_exit_1():
    out = io.StringIO()

    async def go():
        async with rover() as url:
            # drive while E-STOP is latched is refused, and `stop` must then not be sent
            steps = sendcmd.parse_steps(
                ["estop", "--", "drive", "dist_m=0.1", "speed=0.5", "--", "stop"]
            )
            return await sendcmd.run_steps(url, steps, out)

    assert run(go()) == 1
    assert "err E_ESTOP" in out.getvalue() and "> stop" not in out.getvalue()


def test_unreachable_rover_is_exit_1_not_a_traceback(capsys):
    assert sendcmd.main(["--pi", "127.0.0.1", "--port", "1", "snapshot"]) == 1
    assert "!" in capsys.readouterr().err
