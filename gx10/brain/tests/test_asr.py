import os
import subprocess
import sys

import httpx
import pytest

from brain.voice import asr


def test_grok_request_shape(monkeypatch):
    seen = {}

    def fake_post(url, headers, data, files, timeout):
        seen.update(url=url, auth=headers["Authorization"], data=data, file=files["file"])
        return httpx.Response(
            200, json={"text": " three float rocks "}, request=httpx.Request("POST", url)
        )

    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setattr(asr.httpx, "post", fake_post)
    text, source = asr.transcribe_ex(b"RIFF....", "audio/wav")
    assert (text, source) == ("three float rocks", "grok")
    assert seen["url"] == "https://api.x.ai/v1/stt" and seen["auth"] == "Bearer xai-test"
    assert seen["data"]["model"] == "grok-voice-transcribe-2.0"
    assert "float rock" in seen["data"]["keyterm"]
    assert seen["file"][0] == "mission.wav"


def test_falls_back_then_reports_unavailable(monkeypatch):
    def offline(*a, **k):
        raise httpx.ConnectError("no internet")

    monkeypatch.setattr(asr.httpx, "post", offline)
    monkeypatch.setattr(
        asr, "_whisper", lambda audio: (_ for _ in ()).throw(asr.AsrUnavailable("not installed"))
    )
    with pytest.raises(asr.AsrUnavailable):
        asr.transcribe(b"x")
    monkeypatch.setattr(asr, "_whisper", lambda audio: "come back")
    assert asr.transcribe_ex(b"x") == ("come back", "faster-whisper")


@pytest.mark.skipif(
    os.environ.get("XAI_LIVE") != "1" or sys.platform != "darwin",
    reason="set XAI_LIVE=1 on macOS (uses `say` to make audio) to call the real Grok STT",
)
def test_live_grok_stt(tmp_path):
    aiff, wav = tmp_path / "m.aiff", tmp_path / "m.wav"
    subprocess.run(["say", "-o", str(aiff), "Find three float rocks and come back."], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", str(aiff), str(wav)], check=True)
    text, source = asr.transcribe_ex(wav.read_bytes(), "audio/wav")
    assert source == "grok" and "float rock" in text.lower()
