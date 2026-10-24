import json

from marsrover_shared import eventlog


def test_log_appends_contract_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    eventlog.log(
        "r_20261024_101500", "brain", "info", "fsm", "IDLE -> SURVEY", {"station": "s_0001"}
    )
    eventlog.log("r_20261024_101500", "brain", "warn", "stdb_backpressure", "dropped 3")
    path = tmp_path / "runs" / "r_20261024_101500" / "events.jsonl"
    lines = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert [ln["kind"] for ln in lines] == ["fsm", "stdb_backpressure"]
    assert set(lines[0]) == {"t", "run_id", "src", "level", "kind", "msg", "data"}
    assert eventlog.read("r_20261024_101500")[1].data == {}
