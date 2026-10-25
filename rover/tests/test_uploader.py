"""Uploader: the doc 00 6.4-6.5 multipart, one-try semantics, rejection, and the retry thread."""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from test_local_store import FILES, RUN, sample, station

from rover.local_store import LocalStore
from rover.uploader import Uploader, UploadFailed, multipart

MAPPER = "http://mapper.test:8200"


def station_reply(sid: str = "s_0001", ok: bool = True) -> dict:
    return {
        "ok": ok,
        "station_id": sid,
        "stored": f"/data/runs/{RUN}/stations/{sid}/",
        "urls": {"bev": f"{MAPPER}/files/runs/{RUN}/stations/{sid}/bev.png", "crops": {}},
    }


class FakeMapper:
    """httpx.MockTransport handler: records requests, answers from a script of status codes."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            status = self.statuses.pop(0) if self.statuses else 200
        if status == -1:
            raise httpx.ConnectError("mapper unreachable", request=request)
        if request.url.path == "/ingest/sample":
            return httpx.Response(status, json={"ok": True, "url": f"{MAPPER}/files/x.jpg"})
        return httpx.Response(status, json=station_reply())

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def make(tmp_path, mapper: FakeMapper, events: list | None = None, **kw) -> Uploader:
    store = LocalStore(tmp_path)
    emit = (lambda kind, detail: events.append((kind, detail))) if events is not None else None
    return Uploader(store, MAPPER, emit, transport=mapper.transport, **kw)


# --- the wire format -----------------------------------------------------------------------


def test_multipart_puts_metadata_first_and_names_parts_after_files():
    parts = multipart("station.json", "{}", {"kf_000.jpg": b"j", "bev.png": b"p"})
    assert [p[0] for p in parts] == ["station.json", "bev.png", "kf_000.jpg"]
    assert parts[0][1][2] == "application/json"
    assert parts[1][1][2] == "image/png" and parts[2][1][2] == "image/jpeg"


def test_station_upload_posts_every_part(tmp_path):
    mapper = FakeMapper()
    up = make(tmp_path, mapper)
    rel = up.store.save_station(station(), FILES)
    reply = up.upload_station(rel)
    assert reply.ok and reply.station_id == "s_0001"
    (req,) = mapper.requests
    assert str(req.url) == f"{MAPPER}/ingest/station"
    body = req.read()
    for name in ("station.json", "kf_000.jpg", "bev.png"):
        assert f'name="{name}"; filename="{name}"'.encode() in body
    assert up.backlog == 0


def test_sample_upload(tmp_path):
    mapper = FakeMapper()
    up = make(tmp_path, mapper)
    rel = up.store.save_sample(sample(), b"\xff\xd8")
    assert up.upload_sample(rel).url.endswith("x.jpg")
    assert mapper.requests[0].url.path == "/ingest/sample"


# --- failures ------------------------------------------------------------------------------


def test_unreachable_mapper_keeps_it_pending_and_tells_the_brain(tmp_path):
    events: list = []
    up = make(tmp_path, FakeMapper(-1), events)
    rel = up.store.save_station(station(), FILES)
    with pytest.raises(UploadFailed):
        up.upload_station(rel)
    assert up.backlog == 1 and up.store.status(rel)[:2] == (0, 1)
    ((kind, detail),) = events
    assert kind == "upload_retry" and detail["backlog"] == 1 and not detail["rejected"]


@pytest.mark.parametrize("status", [500, 503, 408, 429])
def test_transient_statuses_are_retried(tmp_path, status):
    up = make(tmp_path, FakeMapper(status))
    rel = up.store.save_station(station(), FILES)
    with pytest.raises(UploadFailed):
        up.upload_station(rel)
    assert up.store.pending_uploads() == [rel]


def test_a_4xx_is_rejected_so_it_cannot_block_the_queue(tmp_path):
    up = make(tmp_path, FakeMapper(422))
    rel = up.store.save_station(station(), FILES)
    with pytest.raises(UploadFailed):
        up.upload_station(rel)
    assert up.backlog == 0 and up.store.status(rel)[0] == -1
    assert (tmp_path / rel / "bev.png").exists()  # files kept for a human


def test_ok_false_is_not_a_delivery(tmp_path):
    class NotOk(FakeMapper):
        def __call__(self, request):
            super().__call__(request)
            return httpx.Response(200, json=station_reply(ok=False))

    up = make(tmp_path, NotOk())
    rel = up.store.save_station(station(), FILES)
    with pytest.raises(UploadFailed):
        up.upload_station(rel)
    assert up.backlog == 1


# --- the retry thread ----------------------------------------------------------------------


def wait_for(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_retry_thread_drains_after_the_mapper_comes_back(tmp_path):
    mapper = FakeMapper(-1, -1, -1)  # down for the first three tries, then up
    up = make(tmp_path, mapper, retry_min_s=0.01, retry_max_s=0.04)
    rel = up.store.save_station(station(), FILES)
    with pytest.raises(UploadFailed):
        up.upload_station(rel)
    up.start()
    try:
        assert wait_for(lambda: up.backlog == 0)
    finally:
        up.stop()
    assert len(mapper.requests) == 4  # 1 op attempt + 2 failed retries + 1 success


def test_retry_thread_picks_up_a_backlog_from_before_a_reboot(tmp_path):
    LocalStore(tmp_path).save_station(station(), FILES)  # saved, never uploaded, then reboot
    mapper = FakeMapper()
    up = make(tmp_path, mapper, retry_min_s=0.01)
    assert up.backlog == 1
    up.start()
    try:
        assert wait_for(lambda: up.backlog == 0)
    finally:
        up.stop()


def test_backoff_doubles_and_caps(tmp_path, monkeypatch):
    up = make(tmp_path, FakeMapper(*[-1] * 10), retry_min_s=1.0, retry_max_s=4.0)
    up.store.save_station(station(), FILES)
    waits: list[float] = []

    def fake_wait(seconds: float) -> bool:
        waits.append(seconds)
        return len(waits) >= 6  # "stop" after six waits

    monkeypatch.setattr(up._stop, "wait", fake_wait)
    up.retry_loop()
    assert waits == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]


def test_stop_is_prompt_even_mid_backoff(tmp_path):
    up = make(tmp_path, FakeMapper(*[-1] * 10), retry_min_s=30.0)
    up.store.save_station(station(), FILES)
    up.start()
    t0 = time.monotonic()
    up.stop()
    assert time.monotonic() - t0 < 1.0
