"""Full campaign against P3's real mocks and the local SpacetimeDB.

The unit tests drive StubVision and StubMapper. This one is the I2 check: the same
FSM and the same pydantic clients talk to mocks/fake_vision.py (real Bradford
simulator) and mocks/fake_mapper.py, and both services write the published module.

Skipped when SpacetimeDB is not reachable or the writer tokens are unset.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from brain import settings
from brain.fsm import RunFSM
from brain.rover_link import RoverLink
from brain.stdb_writer import BrainDb
from brain.tests.stub_rover import StubRover
from brain.vision_client import MapperClient, VisionClient, load_priors
from marsrover_shared.ids import IdGen
from marsrover_shared.stdb_http import RateLimitedWriter, StdbClient
from vision.bradford_sim import simulate

_ROOT = Path(__file__).resolve().parents[3]


def _stdb_ready() -> bool:
    try:
        settings.load_env()
        url = os.environ.get("STDB_URL", "")
        tokens = os.environ.get("STDB_TOKEN_BRAIN") and os.environ.get("STDB_TOKEN_MAPPER")
        if not url or not tokens:
            return False
        return httpx.get(url.rstrip("/") + "/v1/ping", timeout=1.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _stdb_ready(), reason="local SpacetimeDB + writer tokens")


def _load_mock(mod_name: str, filename: str):
    path = _ROOT / "mocks" / filename
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve(app, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            return server, task
        await asyncio.sleep(0.01)
    raise RuntimeError(f"server on {port} did not start")


class TracingDb(BrainDb):
    def __init__(self, client: StdbClient) -> None:
        super().__init__(client)
        self.events: list[tuple[str, str, str]] = []

    def event(self, kind: str, msg: str, level: str = "info", data: dict | None = None) -> None:
        self.events.append((kind, level, msg))
        super().event(kind, msg, level, data)


def _mission(run_id: str) -> dict:
    return {
        "mission_id": "m_0001",
        "run_id": run_id,
        "text": "Survey within 1.5 m and sample the three best rocks.",
        "radius_m": 1.5,
        "k": 3,
        "categories": ["float_rock"],
        "sample": True,
        "return_home": True,
        "max_duration_s": 900,
        "mode": "act",
        "assumptions": [],
    }


def _run_campaign(tmp_path, monkeypatch, *, empty_rate: float, outcomes: list[str], notes: str):
    """One full campaign. empty_rate=0 keeps every scoop; 1 forces the retry path."""
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))

    async def go():
        settings.load_env()
        # Offset so two tests in the same second get different run ids.
        offset = 0 if empty_rate == 0 else 1
        run_id = (datetime.now() + timedelta(seconds=offset)).strftime("r_%Y%m%d_%H%M%S")
        vision_port, mapper_port = _free_port(), _free_port()
        public = f"http://127.0.0.1:{mapper_port}"
        tag = "full" if empty_rate == 0 else "empty"
        vision_mod = _load_mock(f"fake_vision_{tag}", "fake_vision.py")
        mapper_mod = _load_mock(f"fake_mapper_{tag}", "fake_mapper.py")
        mapper_writer = RateLimitedWriter(
            StdbClient.from_env("STDB_TOKEN_MAPPER"), hz={}, max_queue=4000
        )
        vision_app = vision_mod.create_app(empty_rate=empty_rate)
        mapper_app = mapper_mod.create_app(tmp_path, public, mapper_writer)
        servers: list[tuple[uvicorn.Server, asyncio.Task]] = []
        vision = VisionClient(f"http://127.0.0.1:{vision_port}")
        mapper = MapperClient(public)
        db = TracingDb(StdbClient.from_env("STDB_TOKEN_BRAIN"))
        try:
            servers.append(await _serve(vision_app, vision_port))
            servers.append(await _serve(mapper_app, mapper_port))
            async with httpx.AsyncClient() as http:
                assert (await http.get(f"http://127.0.0.1:{vision_port}/health")).status_code == 200
                health = (await http.get(f"{public}/health")).json()
                assert health["ok"] is True and health["stdb"] is True

            async with StubRover(mapper_url=public) as rover:
                link = RoverLink(rover.url)
                link.start()
                for _ in range(150):
                    if link.connected and link.last_telemetry:
                        break
                    await asyncio.sleep(0.02)
                fsm = RunFSM(link, vision, db, load_priors(), mapper)
                db.run_started(run_id, notes)
                await fsm.start_mission(_mission(run_id), IdGen())
                await link.stop()

            errors = [e for e in db.events if e[1] == "error"]
            assert fsm.state == "DONE", (fsm.state, errors)
            assert not errors
            assert rover.ops[0] == "start_run" and rover.run_id == run_id
            assert len(fsm.cands) == 5
            assert any(len(c.prop_ids) > 1 for c in fsm.cands.values())
            assert [v.outcome for v in fsm.visits] == outcomes
            assert all(
                len(c.reason) <= 140 and c.thumb_url.startswith(public) for c in fsm.cands.values()
            )
            assert all(len(emb) == 16 for emb in fsm.embeddings.values())
            if empty_rate == 1:
                assert rover.ops.count("scoop") == 6  # one retry on each of k targets

            async with httpx.AsyncClient() as http:
                for c in fsm.cands.values():
                    resp = await http.get(c.thumb_url)
                    assert resp.status_code == 200 and resp.content[:2] == b"\xff\xd8", c.thumb_url
        finally:
            await vision.close()
            await mapper.close()
            for server, _task in servers:
                server.should_exit = True
            for _server, task in servers:
                await task
            db._w.close(timeout_s=30)
            mapper_writer.close(timeout_s=30)

        assert db._w.dropped == 0, db.events
        assert mapper_writer.dropped == 0
        brain = StdbClient.from_env("STDB_TOKEN_BRAIN")
        try:
            runs = brain.sql(f"SELECT * FROM run WHERE run_id = '{run_id}'")
            assert len(runs) == 1, runs
            assert runs[0]["status"] == "done" and runs[0]["fused"] is True, runs[0]

            cands = brain.sql(f"SELECT * FROM candidate WHERE run_id = '{run_id}'")
            assert len(cands) == 5
            visits = brain.sql(f"SELECT * FROM visit WHERE run_id = '{run_id}'")
            assert sorted(v["outcome"] for v in visits) == sorted(outcomes)

            analyses = brain.sql(f"SELECT * FROM analysis_record WHERE run_id = '{run_id}'")
            assert len(analyses) == 3
            for rec in analyses:
                assert rec["simulated"] is True
                assert rec["image_url"].startswith(f"{public}/files/runs/{run_id}/samples/")
                if empty_rate == 0:
                    interest = min(max(fsm.cands[rec["cand_id"]].score, 0.0), 1.0)
                    expected = simulate(rec["sample_id"], run_id, interest)
                    assert rec["scoop_has_item"] is True
                    assert rec["assay_type"] == "bradford_sim"
                    assert rec["verdict"] == expected["verdict"]
                    assert rec["seed"] == expected["seed"]
                    assert rec["a_595"] == expected["a595"]
                    assert rec["concentration"] == expected["concentration"]
                    assert rec["model_version"] == expected["model_version"]
                else:
                    assert rec["scoop_has_item"] is False and rec["label"] == "empty"
                    assert rec["assay_type"] == "" and rec["verdict"] == ""
                    assert rec["a_595"] == -1 and rec["concentration"] == -1
                    assert rec["seed"] == 0 and rec["model_version"] == ""

            kfs = brain.sql(f"SELECT heading_deg FROM keyframe WHERE run_id = '{run_id}'")
            assert len(kfs) == 40  # 5 stations × 8 views
            tiles = brain.sql(f"SELECT * FROM local_map_tile WHERE run_id = '{run_id}'")
            assert len(tiles) == 5
            global_tiles = brain.sql(f"SELECT * FROM global_tile WHERE run_id = '{run_id}'")
            assert len(global_tiles) == 1
            g = global_tiles[0]
            assert (g["origin_x"], g["origin_y"], g["version"]) == (1.28, 1.28, 1)
        finally:
            brain.close()

    asyncio.run(asyncio.wait_for(go(), 90))


def test_campaign_against_p3_mocks_lands_in_stdb(tmp_path, monkeypatch):
    _run_campaign(
        tmp_path,
        monkeypatch,
        empty_rate=0.0,
        outcomes=["sampled"] * 3,
        notes="p3 mock campaign",
    )


def test_empty_scoops_against_p3_vision_retry_once(tmp_path, monkeypatch):
    _run_campaign(
        tmp_path,
        monkeypatch,
        empty_rate=1.0,
        outcomes=["empty_scoop"] * 3,
        notes="p3 empty scoops",
    )
