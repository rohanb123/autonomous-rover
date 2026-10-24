"""Acceptance tests for the published "marsrover" module (docs/02 §12).

Skipped unless STDB_TEST_URL and STDB_TEST_TOKEN (a writer token) are set, e.g.:
    set -a; source config/.env; set +a
    STDB_TEST_URL=$STDB_URL STDB_TEST_TOKEN=$STDB_TOKEN_BRAIN pytest shared/py/tests -k live
"""

import json
import os
import time

import pytest

from marsrover_shared.stdb_http import StdbClient, StdbError

URL = os.environ.get("STDB_TEST_URL")
DB = os.environ.get("STDB_TEST_DB", "marsrover")
pytestmark = pytest.mark.skipif(not URL, reason="set STDB_TEST_URL + STDB_TEST_TOKEN")

GOLDEN_CURVE = {
    "standard": "BSA",
    "wavelength_nm": 595,
    "linear_range": [125.0, 1000.0],
    "fit": {"type": "linear", "slope": 0.0007188, "intercept": 0.0557, "r2": 0.9947},
}


@pytest.fixture(scope="module")
def db():
    return StdbClient(URL, DB, os.environ["STDB_TEST_TOKEN"])


@pytest.fixture
def run_id(db):
    rid = f"r_live_{time.time_ns()}"
    db.call("start_run", rid, "pytest", time.time())
    return rid


def _analysis(run_id, sample_id, simulated=True, verdict="low"):
    # Positional order of add_analysis (docs/02 §6).
    return [
        run_id, sample_id, "c_0001", "v_0001", time.time(), "http://x/present.jpg",
        "float_rock", 0.62, "A small angular dark rock fragment.", True,
        "bradford_sim", 0.2585, 282.1, "µg/mL", verdict, True,
        json.dumps(GOLDEN_CURVE), simulated, 3896889563, "bradford_sim-1",
    ]  # fmt: skip


def test_no_token_is_refused(run_id):
    anon = StdbClient(URL, DB, "")
    with pytest.raises(StdbError):
        anon.call("insert_pose", run_id, time.time(), 0.0, 0.0, 0.0, "dr", -1.0)


def test_duplicate_run_is_refused(db, run_id):
    with pytest.raises(StdbError):
        db.call("start_run", run_id, "again", time.time())


def test_analysis_must_be_simulated(db, run_id):
    with pytest.raises(StdbError):
        db.call("add_analysis", *_analysis(run_id, f"a_{time.time_ns()}", simulated=False))
    with pytest.raises(StdbError):
        db.call("add_analysis", *_analysis(run_id, f"a_{time.time_ns()}", verdict="spicy"))


def test_golden_assay_round_trips_exactly_and_is_idempotent(db, run_id):
    sample_id = f"a_{time.time_ns()}"
    db.call("add_analysis", *_analysis(run_id, sample_id))
    db.call("add_analysis", *_analysis(run_id, sample_id))  # same sampleId -> update, not insert
    rows = db.sql(f"SELECT * FROM analysis_record WHERE sample_id = '{sample_id}'")
    assert len(rows) == 1
    r = rows[0]
    assert (r["seed"], r["a_595"], r["concentration"]) == (3896889563, 0.2585, 282.1)
    assert r["simulated"] is True and json.loads(r["standard_curve_params"]) == GOLDEN_CURVE


def test_mark_visited_updates_candidate(db, run_id):
    cand = f"c_{time.time_ns()}"
    db.call(
        "upsert_candidate", cand, run_id, "s_0001", 1.0, 0.2, 0.63, "{}", "float_rock", 0.028,
        "queued", 1, "Novel texture", "", time.time(),
    )  # fmt: skip
    db.call("mark_visited", f"v_{time.time_ns()}", run_id, cand, 1.0, 0.2, 0.20, "unreachable", 0.0)
    (row,) = db.sql(f"SELECT * FROM candidate WHERE cand_id = '{cand}'")
    assert (row["status"], row["rank"]) == ("failed", -1)


def test_global_tile_keeps_newest_version(db, run_id):
    tile = f"g_{run_id}_0_0"
    for version, png in [(2, "new"), (1, "stale")]:
        db.call("upsert_global_tile", tile, run_id, 0, 0, 0.0, 0.0, 0.005, 512, 512, png, version)
    (row,) = db.sql(f"SELECT * FROM global_tile WHERE tile_id = '{tile}'")
    assert (row["version"], row["png_b_64"]) == (2, "new")


def test_size_caps_and_event_truncation(db, run_id):
    with pytest.raises(StdbError):
        db.call("upsert_keyframe", "k_big", run_id, "s_1", 0.0, 0.0, 0.0, 0.0, "x" * 150_001, "")
    db.call("log_event", run_id, time.time(), "brain", "info", "test", "m" * 600, "{}")
    (row,) = db.sql(f"SELECT * FROM event WHERE run_id = '{run_id}'")
    assert len(row["msg"]) == 500
