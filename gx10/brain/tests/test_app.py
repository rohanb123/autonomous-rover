import pytest
from fastapi.testclient import TestClient

from brain import app as app_mod
from brain.voice import asr, nlu


class FakeDb:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))


@pytest.fixture
def client(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(app_mod, "BrainDb", lambda client: db)
    monkeypatch.setattr(
        nlu, "_parse_grok", lambda text: (_ for _ in ()).throw(ConnectionError("offline"))
    )
    with TestClient(app_mod.app) as c:
        c.db = db
        yield c


def test_mission_needs_a_run(client):
    r = client.post("/api/mission/text", json={"text": "find 2 rocks"})
    assert r.status_code == 409


def test_run_then_text_mission(client):
    assert client.post("/api/run/start", json={}).json()["accepted"] is True
    r = client.post(
        "/api/mission/text", json={"text": "find 2 layered rocks within 1 meter and come back"}
    )
    assert r.status_code == 200 and r.json()["cmd_uid"].startswith("u_")
    missions = [a for name, a, _ in client.db.calls if name == "mission"]
    m, status = missions[0]
    assert (m["k"], m["radius_m"], m["categories"], status) == (2, 1.0, ["layered"], "parsed")
    kinds = [a[0] for name, a, _ in client.db.calls if name == "event"]
    assert "voice_offline" in kinds and "mission" in kinds


def test_voice_mission_and_quick_command(client, monkeypatch):
    client.post("/api/run/start", json={})
    monkeypatch.setattr(
        asr, "transcribe_ex", lambda audio, mime: ("find three float rocks", "grok")
    )
    r = client.post("/api/mission/voice", content=b"audio", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 200
    monkeypatch.setattr(asr, "transcribe_ex", lambda audio, mime: ("stop", "grok"))
    r = client.post("/api/mission/voice", content=b"audio", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 503 and "stop" in r.json()["detail"]


def test_voice_unavailable_says_type_instead(client, monkeypatch):
    client.post("/api/run/start", json={})

    def unavailable(audio, mime):
        raise asr.AsrUnavailable("offline backup not installed")

    monkeypatch.setattr(asr, "transcribe_ex", unavailable)
    r = client.post("/api/mission/voice", content=b"audio", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 503 and "Type the mission" in r.json()["detail"]


def test_rover_commands_are_honest_503s(client):
    for path, body in [("/api/estop", {}), ("/api/quick", {"cmd": "pause"})]:
        assert client.post(path, json=body).status_code == 503
