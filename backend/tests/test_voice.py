"""Голос: нормализация диктовки, распознавание через совместимый сервер, голосовая команда."""

from __future__ import annotations

import httpx
import pytest

from bis import voice
from bis.config import get_settings


def test_normalize_dictation():
    assert voice.normalize_dictation("залили 25 кубов бетона в осях 1 по 5") == "залили 25 м3 бетона в осях 1-5"
    assert voice.normalize_dictation("уложили 120 квадратных метров мембраны и 3 тонны арматуры") == \
        "уложили 120 м2 мембраны и 3 т арматуры"
    assert voice.normalize_dictation("кубы бетона привезли") == "кубы бетона привезли"  # без числа не трогаем


@pytest.fixture
def stt(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "stt_url", "http://stt/v1")
    monkeypatch.setattr(s, "tts_url", "http://tts/v1")
    yield s


def test_transcribe_and_speak(stt):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        if request.url.path.endswith("/audio/transcriptions"):
            assert b'name="language"' in request.content and b"ru" in request.content
            return httpx.Response(200, json={"text": "залили 30 кубов"})
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert voice.transcribe(b"audio", client=client) == "залили 30 м3"
    assert voice.speak("Готово", client=client) == (b"ID3mp3", "audio/mpeg")


def test_voice_disabled():
    with pytest.raises(voice.VoiceError):
        voice.transcribe(b"x")


def test_voice_command_runs_agent(client, auth, stt, monkeypatch):
    assert client.get("/api/voice/status", headers=auth).json() == {"stt": True, "tts": True}
    monkeypatch.setattr(voice, "transcribe", lambda *a, **k: "Какая толщина слоя обратной засыпки по СП?")
    r = client.post("/api/voice/command", headers=auth, files={"file": ("rec.webm", b"audio", "audio/webm")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["transcript"].startswith("Какая толщина") and data["agent"] == "norms" and data["citations"]


def test_voice_endpoints_report_unavailable(client, auth):
    r = client.post("/api/voice/transcribe", headers=auth, files={"file": ("rec.webm", b"audio", "audio/webm")})
    assert r.status_code == 503
