import importlib.util
from pathlib import Path

import pytest

from marsrover_shared.stdb_http import StdbError

_spec = importlib.util.spec_from_file_location(
    "replay_run", Path(__file__).resolve().parents[3] / "mocks" / "replay_run.py"
)
replay_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_run)

CALLS = [
    {"t": 0.0, "reducer": "start_run", "args": ["r_old", "notes", 0.0]},
    {"t": 0.1, "reducer": "upsert_rover_status", "args": ["r_old", 0.1, "IDLE"]},
    {
        "t": 0.2,
        "reducer": "upsert_global_tile",
        "args": ["g_r_old_0_1", "r_old", 0, 1, 0.0, 0.0, 0.005, 512, 512, "png", 1],
    },
    {
        "t": 0.3,
        "reducer": "upsert_keyframe",
        "args": ["k_0001", "r_old", "s_0001", 0.3, 0, 0, 0, "b64", "http://m/runs/r_old/k.jpg"],
    },
    {"t": 0.4, "reducer": "end_run", "args": ["r_old", "done", 0.4]},
]


class Client:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def call(self, reducer, *args):
        if reducer in self.fail:
            raise StdbError(530, self.fail[reducer])
        self.calls.append((reducer, args))


def test_plan_renames_ids_but_not_urls_and_skips_status():
    todo = replay_run.plan(CALLS, "r_new", "r_old", with_status=False)
    assert [c["reducer"] for c in todo] == [
        "start_run",
        "upsert_global_tile",
        "upsert_keyframe",
        "end_run",
    ]
    assert todo[0]["args"][0] == "r_new"
    assert todo[1]["args"][:2] == ["g_r_new_0_1", "r_new"]
    assert todo[2]["args"][-1] == "http://m/runs/r_old/k.jpg"  # file URLs keep the original run
    assert len(replay_run.plan(CALLS, None, "r_old", with_status=True)) == 5


def test_replay_sends_in_order_and_reports_failures():
    client = Client(fail={"upsert_keyframe": "thumbB64 too big"})
    sent = replay_run.replay(
        client, replay_run.plan(CALLS, None, "r_old", False), log=lambda m: None
    )
    assert [r for r, _ in client.calls] == ["start_run", "upsert_global_tile", "end_run"]
    assert sent["FAILED upsert_keyframe"] == 1 and sent["start_run"] == 1


def test_existing_run_stops_with_a_clear_hint():
    with pytest.raises(SystemExit) as e:
        replay_run.replay(Client(fail={"start_run": "run r_old already exists"}), CALLS)
    assert "--as" in str(e.value)


def test_speed_paces_by_recorded_time(monkeypatch):
    slept = []
    monkeypatch.setattr(replay_run.time, "sleep", slept.append)
    replay_run.replay(Client(), CALLS, speed=2.0)
    assert slept == pytest.approx([0.0, 0.05, 0.05, 0.05, 0.05])
