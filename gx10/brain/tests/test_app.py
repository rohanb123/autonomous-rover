import pytest
from fastapi.testclient import TestClient

from brain import app as app_mod
from brain.voice import asr, nlu


class FakeDb:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))


class FakeLink:
    def __init__(self, url):
        self.connected, self.last_telemetry = True, None
        self.on_telemetry = self.on_event = self.on_connection = None

    def start(self):
        pass

    async def stop(self):
        pass


class FakeVision:
    def __init__(self, url):
        pass

    async def close(self):
        pass


class FakeOverhead:
    def __init__(self, url):
        self.health = "down"

    def start(self):
        pass

    async def stop(self):
        pass


class FakeFsm:
    def __init__(self, link, vision, db, priors, mapper=None, overhead=None):
        self.state, self.active_cand, self.cands, self.busy = "IDLE", "", {}, False
        self.calls = []
        self.on_run_ended = None

    def skip(self):
        self.calls.append(("skip",))

    def come_back(self):
        self.calls.append(("come_back",))

    def approve(self, mission_id, approve):
        from brain.fsm import NotNow

        if mission_id != "m_0001":
            raise NotNow("not awaiting")
        self.calls.append(("approve", approve))

    def start_mission(self, mission, ids):
        self.calls.append(("start", mission["mission_id"]))

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))

    async def stop(self):
        self.calls.append(("stop",))

    async def estop(self):
        self.calls.append(("estop",))

    async def clear_estop(self):
        self.calls.append(("clear_estop",))


@pytest.fixture
def client(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(app_mod, "BrainDb", lambda client: db)
    monkeypatch.setattr(app_mod, "RoverLink", FakeLink)
    monkeypatch.setattr(app_mod, "VisionClient", FakeVision)
    monkeypatch.setattr(app_mod, "MapperClient", FakeVision)
    monkeypatch.setattr(app_mod, "OverheadLink", FakeOverhead)
    monkeypatch.setattr(app_mod, "RunFSM", FakeFsm)
    monkeypatch.setattr(
        nlu, "_parse_grok", lambda text: (_ for _ in ()).throw(ConnectionError("offline"))
    )
    with TestClient(app_mod.app) as c:
        c.db, c.fsm = db, c.app.state.brain.fsm
        yield c


def test_mission_needs_a_run(client):
    assert client.post("/api/mission/text", json={"text": "find 2 rocks"}).status_code == 409


def test_text_mission_starts_the_campaign(client):
    client.post("/api/run/start", json={})
    r = client.post(
        "/api/mission/text", json={"text": "find 2 layered rocks within 1 meter and come back"}
    )
    assert r.status_code == 200 and r.json()["cmd_uid"].startswith("u_")
    m, status = next(a for name, a, _ in client.db.calls if name == "mission")
    assert (m["k"], m["radius_m"], m["categories"], status) == (2, 1.0, ["layered"], "parsed")
    assert client.fsm.calls == [("start", "m_0001")]


def test_voice_mission_and_voice_quick_command(client, monkeypatch):
    client.post("/api/run/start", json={})
    monkeypatch.setattr(
        asr, "transcribe_ex", lambda audio, mime: ("find three float rocks", "grok")
    )
    assert (
        client.post(
            "/api/mission/voice", content=b"a", headers={"Content-Type": "audio/webm"}
        ).status_code
        == 200
    )
    monkeypatch.setattr(asr, "transcribe_ex", lambda audio, mime: ("stop", "grok"))
    assert (
        client.post(
            "/api/mission/voice", content=b"a", headers={"Content-Type": "audio/webm"}
        ).status_code
        == 200
    )
    assert client.fsm.calls[-1] == ("stop",)


def test_voice_unavailable_says_type_instead(client, monkeypatch):
    client.post("/api/run/start", json={})

    def unavailable(audio, mime):
        raise asr.AsrUnavailable("offline backup not installed")

    monkeypatch.setattr(asr, "transcribe_ex", unavailable)
    r = client.post("/api/mission/voice", content=b"a", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 503 and "Type the mission" in r.json()["detail"]


def test_rover_controls(client):
    for body in ({"cmd": "pause"}, {"cmd": "resume"}, {"cmd": "stop"}):
        assert client.post("/api/quick", json=body).status_code == 200
    assert client.post("/api/estop", json={}).status_code == 200
    assert client.post("/api/estop/clear", json={}).status_code == 200
    for body in ({"cmd": "skip"}, {"cmd": "come_back"}):
        assert client.post("/api/quick", json=body).status_code == 200
    ok = client.post("/api/approve", json={"mission_id": "m_0001", "approve": True})
    assert ok.status_code == 200
    bad = client.post("/api/approve", json={"mission_id": "m_0009", "approve": True})
    assert bad.status_code == 409
    calls = [c[0] for c in client.fsm.calls]
    assert calls == [
        "pause",
        "resume",
        "stop",
        "estop",
        "clear_estop",
        "skip",
        "come_back",
        "approve",
    ]
    assert client.get("/api/health").json()["rover_link"] == "ok"
