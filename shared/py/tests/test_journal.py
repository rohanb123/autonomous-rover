import time

from marsrover_shared import journal
from marsrover_shared.stdb_http import RateLimitedWriter


def test_run_of_and_record_read(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    assert journal.run_of("insert_pose", ["r_1", 1.0]) == "r_1"
    assert journal.run_of("upsert_candidate", ["c_0001", "r_1"]) == "r_1"
    assert journal.run_of("claim_writer", ["brain"]) == ""  # setup, not run data
    journal.record("start_run", ("r_1", "notes", 1.0))
    journal.record("upsert_mission", ("m_0001", "r_1", "text", "{}", "[]", "parsed"))
    journal.record("add_writer", ("abc", "mapper"))  # ignored
    calls = journal.read("r_1")
    assert [c["reducer"] for c in calls] == ["start_run", "upsert_mission"]
    assert calls[1]["args"][0] == "m_0001" and calls[0]["t"] <= calls[1]["t"]
    assert journal.path("r_1") == tmp_path / "runs" / "r_1" / "stdb_calls.jsonl"


class Client:
    def __init__(self):
        self.calls = []

    def call(self, reducer, *args):
        self.calls.append(reducer)


def test_writer_journals_every_submit_even_coalesced():
    seen = []
    client = Client()
    w = RateLimitedWriter(client, {"pose": 2}, journal=lambda r, a: seen.append((r, a)))
    for i in range(10):
        w.submit("pose", "insert_pose", "r_1", float(i))
    w.submit("e", "log_event", "r_1", "x")
    time.sleep(0.05)
    w.close()
    assert len(seen) == 11  # the journal saw all of them
    assert client.calls.count("insert_pose") < 10  # the DB got the coalesced subset


def test_journal_failure_never_breaks_the_live_write():
    client = Client()

    def broken(reducer, args):
        raise OSError("disk full")

    w = RateLimitedWriter(client, {}, journal=broken)
    w.submit("e", "log_event", "r_1", "x")
    w.close()
    assert client.calls == ["log_event"]
