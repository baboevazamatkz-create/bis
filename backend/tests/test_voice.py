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


@pytest.mark.parametrize('status', [200, 503])
def test_owned_voice_clients_are_closed_on_success_and_error(stt, monkeypatch, status):
    original = httpx.Client
    clients = []

    def handler(request):
        if request.url.path.endswith('transcriptions'):
            return httpx.Response(status, json={'text': 'готово'})
        return httpx.Response(status, content=b'audio')

    def create(**kwargs):
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, 'Client', create)
    for action in (lambda: voice.transcribe(b'audio'), lambda: voice.speak('готово')):
        if status == 503:
            with pytest.raises(voice.VoiceError):
                action()
        else:
            action()
    assert len(clients) == 2 and all(client.is_closed for client in clients)


def test_official_openai_chat_enables_whisper_without_separate_stt_url(monkeypatch):
    from bis.config import Settings

    settings = Settings(llm_providers_json='''[
        {"name":"openai","kind":"openai_compat","base_url":"https://api.openai.com/v1",
         "api_key_env":"BIS_TEST_OPENAI_KEY"}
    ]''', llm_routes_json='{"default":["openai"]}')
    monkeypatch.setattr(voice, "get_settings", lambda: settings)
    monkeypatch.setenv("BIS_TEST_OPENAI_KEY", "test-only")

    def handler(request):
        assert request.url == "https://api.openai.com/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer test-only"
        assert b"whisper-1" in request.content
        return httpx.Response(200, json={"text": "залили 25 кубов"})

    assert voice.status()["stt"] is True
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert voice.transcribe(b"audio", client=client) == "залили 25 м3"


def test_openai_fallback_does_not_override_separate_speechkit(monkeypatch):
    from bis.config import Settings

    settings = Settings(stt_provider="yandex", llm_providers_json='''[
        {"name":"openai","kind":"openai_compat","base_url":"https://api.openai.com/v1","api_key":"test-only"}
    ]''')
    monkeypatch.setattr(voice, "get_settings", lambda: settings)
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    assert voice.status()["stt"] is False


def test_unselected_or_compatible_chat_does_not_enable_whisper(monkeypatch):
    from bis.config import Settings

    settings = Settings(llm_providers_json='''[
        {"name":"claude","kind":"anthropic","api_key":"test-only"},
        {"name":"compatible","kind":"openai_compat","base_url":"https://example.com/v1","api_key":"test-only"},
        {"name":"openai","kind":"openai_compat","base_url":"https://api.openai.com/v1","api_key":"test-only"}
    ]''', llm_routes_json='{"default":["claude","openai"]}')
    monkeypatch.setattr(voice, "get_settings", lambda: settings)
    assert voice.status()["stt"] is False
    settings.llm_routes_json = '{"default":["compatible","openai"]}'
    assert voice.status()["stt"] is False


def test_voice_settings_are_separate_from_llm_and_ocr(client, auth, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("BIS_OCR_PROVIDER=anthropic\n")
    monkeypatch.setenv("BIS_LOCAL_DIR", str(tmp_path))
    for key in ("BIS_STT_PROVIDER", "BIS_STT_URL", "BIS_STT_MODEL", "BIS_STT_API_KEY",
                "BIS_STT_YANDEX_FOLDER_ID", "BIS_STT_YANDEX_API_KEY"):
        monkeypatch.setenv(key, "")
    try:
        response = client.post("/api/voice/setup", headers=auth, json={"provider": "openai", "api_key": "voice-only-key"})
        assert response.status_code == 200, response.text
        assert response.json()["stt"] is True
        assert "voice-only-key" not in response.text
        assert get_settings().stt_api_key == "voice-only-key"
        assert "BIS_OCR_PROVIDER=anthropic\n" in env_file.read_text()
        assert "BIS_LLM_CONFIG_FILE" not in env_file.read_text()

        monkeypatch.setattr(voice, "ffmpeg_exe", lambda: "ffmpeg")
        response = client.post("/api/voice/setup", headers=auth, json={
            "provider": "yandex", "folder_id": "voice-folder", "api_key": "speech-only-key"
        })
        assert response.status_code == 200, response.text
        assert response.json()["stt"] is True
        settings = get_settings()
        assert settings.stt_api_key == "voice-only-key"
        assert settings.stt_yandex_api_key == "speech-only-key"
        assert settings.stt_yandex_folder_id == "voice-folder"
        assert settings.stt_url == "https://api.openai.com/v1"  # dormant under SpeechKit
        assert "voice-folder" not in settings.yandex()
    finally:
        get_settings.cache_clear()
