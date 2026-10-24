import json
import os
import threading
import time

import httpx
import pytest

from marsrover_shared.stdb_http import RateLimitedWriter, StdbClient, StdbError


def _client(handler):
    return StdbClient(
        "http://stdb:3000/", "marsrover", "tok", transport=httpx.MockTransport(handler)
    )


def test_call_posts_positional_args_with_bearer():
    seen = {}

    def handler(req):
        seen.update(
            url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content)
        )
        return httpx.Response(200)

    _client(handler).call("insert_pose", "r_1", 1.5, 0.1, -0.2, 30.0, "dr", -1.0)
    assert seen == {
        "url": "http://stdb:3000/v1/database/marsrover/call/insert_pose",
        "auth": "Bearer tok",
        "body": ["r_1", 1.5, 0.1, -0.2, 30.0, "dr", -1.0],
    }


def test_call_raises_on_non_2xx():
    c = _client(lambda req: httpx.Response(530, text="not a writer"))
    with pytest.raises(StdbError) as e:
        c.call("insert_pose")
    assert e.value.status == 530 and "not a writer" in e.value.body


def test_call_retries_connection_errors_only():
    attempts = []

    def flaky(req):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200)

    _client(flaky).call("log_event")
    assert len(attempts) == 3

    timeouts = []

    def slow(req):
        timeouts.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(httpx.ReadTimeout):
        _client(slow).call("insert_pose")
    assert len(timeouts) == 1  # the reducer may have run; never resend


def test_sql_rows_as_dicts():
    body = [
        {
            "schema": {"elements": [{"name": {"some": "run_id"}}, {"name": {"some": "x"}}]},
            "rows": [["r_1", 0.5], ["r_1", 0.6]],
        }
    ]
    c = _client(lambda req: httpx.Response(200, json=body))
    assert c.sql("SELECT run_id, x FROM pose") == [
        {"run_id": "r_1", "x": 0.5},
        {"run_id": "r_1", "x": 0.6},
    ]


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail, self.lock = [], fail, threading.Lock()

    def call(self, reducer, *args):
        if self.fail:
            raise StdbError(500, "boom")
        with self.lock:
            self.calls.append((reducer, args))


def test_writer_coalesces_rate_limited_keys():
    fake = FakeClient()
    w = RateLimitedWriter(fake, {"pose": 5})
    for i in range(50):
        w.submit("pose", "insert_pose", i)
        time.sleep(0.004)
    w.close()
    sent = [a[0] for r, a in fake.calls]
    assert 1 <= len(sent) <= 4  # ~0.2 s of submits at 5 Hz, plus the final flush
    assert sent[-1] == 49  # the latest value always lands


def test_writer_keeps_must_send_writes_in_order():
    fake = FakeClient()
    w = RateLimitedWriter(fake, {})
    for i in range(5):
        w.submit(f"cand{i}", "upsert_candidate", i)
    w.close()
    assert [a[0] for _, a in fake.calls] == [0, 1, 2, 3, 4]


def test_writer_drops_oldest_when_queue_is_full():
    drops = []
    stuck = threading.Event()

    class Blocking(FakeClient):
        def call(self, reducer, *args):
            stuck.wait(2)
            super().call(reducer, *args)

    b = Blocking()
    w3 = RateLimitedWriter(b, {}, max_queue=2, on_drop=lambda r, n: drops.append(n))
    for i in range(6):
        w3.submit("visit", "mark_visited", i)
        time.sleep(0.01)
    stuck.set()
    w3.close()
    assert drops and w3.dropped == len(drops)
    assert [a[0] for _, a in b.calls][-1] == 5


def test_writer_survives_db_errors():
    drops = []
    w = RateLimitedWriter(FakeClient(fail=True), {}, on_drop=lambda r, n: drops.append(r))
    w.submit("e", "log_event", "x")
    w.close()
    assert drops and "log_event" in drops[0]


# --- docs/02 §12.2: against a real server (skipped unless STDB_TEST_URL is set) ---
STDB_TEST_URL = os.environ.get("STDB_TEST_URL")


@pytest.mark.skipif(
    not STDB_TEST_URL, reason="set STDB_TEST_URL (+ STDB_TEST_TOKEN) to run against SpacetimeDB"
)
def test_live_insert_pose_round_trip():
    c = StdbClient(
        STDB_TEST_URL, os.environ.get("STDB_TEST_DB", "marsrover"), os.environ["STDB_TEST_TOKEN"]
    )
    run_id = f"r_test_{int(time.time())}"
    c.call("start_run", run_id, "pytest", time.time())
    for i in range(100):
        c.call("insert_pose", run_id, time.time(), i * 0.01, 0.0, 0.0, "dr", -1.0)
    rows = c.sql(f"SELECT * FROM pose WHERE run_id = '{run_id}'")
    assert len(rows) == 100
    assert rows[0]["source"] == "dr"  # positional args landed in declaration order (U-11)
