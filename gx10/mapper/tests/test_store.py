from __future__ import annotations

import json

import pytest

from ..store import RunStore, StoreError, check_id, check_name
from .helpers import RUN


def test_url_for_and_default_public_url(tmp_path, monkeypatch):
    monkeypatch.delenv("MAPPER_PUBLIC_URL", raising=False)
    s = RunStore(tmp_path)
    assert s.url_for("runs/r/x.png") == "http://192.168.50.10:8200/files/runs/r/x.png"
    monkeypatch.setenv("MAPPER_PUBLIC_URL", "http://localhost:8200/")
    assert RunStore(tmp_path).url_for("/runs/a") == "http://localhost:8200/files/runs/a"
    assert RunStore(tmp_path, "http://h:1").url_for("runs/a") == "http://h:1/files/runs/a"


def test_default_root_follows_marsrover_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    assert RunStore().root == tmp_path


def test_save_station_layout(tmp_path):
    s = RunStore(tmp_path)
    rel = s.save_station(RUN, {"station_id": "s_0001", "x": "µ"}, {"kf_000.jpg": b"j"})
    assert rel == f"runs/{RUN}/stations/s_0001"
    d = tmp_path / rel
    assert (d / "kf_000.jpg").read_bytes() == b"j"
    assert json.loads((d / "station.json").read_text(encoding="utf-8"))["x"] == "µ"
    assert not list(d.glob(".*.tmp"))
    assert s.has_station(RUN, "s_0001") and not s.has_station(RUN, "s_0002")


def test_save_sample_layout(tmp_path):
    rel = RunStore(tmp_path).save_sample(RUN, {"sample_id": "a_0001"}, b"jpg")
    assert rel == f"runs/{RUN}/samples/a_0001/closeup.jpg"
    assert (tmp_path / rel).read_bytes() == b"jpg"


def test_resolve_blocks_traversal_and_missing(tmp_path):
    s = RunStore(tmp_path / "data")
    s.save_sample(RUN, {"sample_id": "a_0001"}, b"x")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    assert s.resolve(f"runs/{RUN}/samples/a_0001/closeup.jpg") is not None
    assert s.resolve("../secret.txt") is None
    assert s.resolve("runs/nope.jpg") is None
    assert s.resolve(f"runs/{RUN}/samples") is None  # directories aren't served


@pytest.mark.parametrize("bad", ["../x", "r_1", "R_20261024", "r_2026/../x", "", "s_00a1"])
def test_check_id_rejects(bad):
    with pytest.raises(StoreError):
        check_id(bad)


@pytest.mark.parametrize("good", [RUN, "s_0001", "a_0001", "k_0123"])
def test_check_id_accepts(good):
    assert check_id(good) == good


@pytest.mark.parametrize("bad", ["../x.jpg", ".hidden", "a/b.jpg", "a\\b.jpg", ""])
def test_check_name_rejects(bad):
    with pytest.raises(StoreError):
        check_name(bad)


def test_bad_file_name_is_not_written(tmp_path):
    with pytest.raises(StoreError):
        RunStore(tmp_path).save_station(RUN, {"station_id": "s_0001"}, {"../evil.jpg": b"x"})
    assert not (tmp_path / "runs" / RUN / "evil.jpg").exists()
