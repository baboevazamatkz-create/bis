"""Голос: распознавание речи (STT) и синтез (TTS).

- stt_provider=openai — OpenAI-совместимый сервер в закрытом контуре (faster-whisper / speaches, vLLM Whisper);
- stt_provider=yandex — Yandex SpeechKit (ключ AI Studio, роль ai.speechkit-stt.user). Аудио перекодируется
  в OGG/Opus и режется на части до 25 с (ограничение синхронного API).

После распознавания текст нормализуется под строительную диктовку: «кубов» → «м3», «квадратов» → «м2» и т.п.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from bis.config import Settings, get_settings

_UNIT_WORDS: list[tuple[str, str]] = [
    (r"\b(?:кубическ\w+ метр\w*|кубометр\w*|куб\w*|м\s?куб\w*)\b", "м3"),
    (r"\b(?:квадратн\w+ метр\w*|квадрат\w*|м\s?кв\w*)\b", "м2"),
    (r"\b(?:погонн\w+ метр\w*|пог\.?\s?метр\w*)\b", "п.м"),
    (r"\bмиллиметр\w*\b", "мм"),
    (r"\bсантиметр\w*\b", "см"),
    (r"\bтонн\w*\b", "т"),
    (r"\bкилограмм\w*\b", "кг"),
    (r"\bштук\w*\b", "шт"),
]
_AXES = re.compile(r"\bв\s+ос(?:ях|и)\s+(\d+)\s+(?:по|до|-)\s+(\d+)", re.IGNORECASE)


def normalize_dictation(text: str) -> str:
    """Числа + единицы в принятую запись; «в осях 1 по 5» → «в осях 1-5»."""
    out = text.strip()
    for pattern, unit in _UNIT_WORDS:
        out = re.sub(r"(\d)\s+" + pattern, rf"\1 {unit}", out, flags=re.IGNORECASE)
    out = _AXES.sub(r"в осях \1-\2", out)
    return re.sub(r"\s{2,}", " ", out)


class VoiceError(Exception):
    pass


OPENAI_STT_URL = "https://api.openai.com/v1"
OPENAI_STT_MODEL = "whisper-1"
YANDEX_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


def _openai_chat_key(settings: Settings) -> str | None:
    try:
        providers = {p.name: p for p in settings.llm_providers()
                     if p.kind != "offline" and "text" in p.capabilities}
        routes = settings.llm_routes()
    except (ValueError, OSError):
        return None
    names = routes.get("chat") or routes.get("default") or providers
    selected = next((providers[name] for name in names if name in providers), None)
    if (selected and selected.kind == "openai_compat" and selected.auth_scheme == "Bearer"
            and (selected.base_url or "").rstrip("/") == OPENAI_STT_URL):
        return selected.resolved_api_key()
    return None


def _openai_stt(settings: Settings) -> tuple[str | None, str | None]:
    if settings.stt_url:
        url = settings.stt_url.rstrip("/")
        key = settings.stt_api_key
        if not key and url == OPENAI_STT_URL:
            key = os.environ.get("BIS_OPENAI_API_KEY") or _openai_chat_key(settings)
        return url, key
    key = _openai_chat_key(settings)
    return (OPENAI_STT_URL, key) if key else (None, None)


def _speechkit_credentials(settings: Settings) -> tuple[str | None, str | None]:
    folder, key = settings.yandex()
    return settings.stt_yandex_folder_id or folder, settings.stt_yandex_api_key or key


def status() -> dict[str, bool]:
    settings = get_settings()
    if settings.stt_provider == "yandex":
        ready = all(_speechkit_credentials(settings)) and bool(ffmpeg_exe())
    else:
        url, key = _openai_stt(settings)
        ready = bool(url and (url != OPENAI_STT_URL or key))
    return {"stt": ready, "tts": bool(settings.tts_url)}


def ffmpeg_exe() -> str | None:
    return get_settings().ffmpeg or shutil.which("ffmpeg")


def _ogg_chunks(audio: bytes, seconds: int = 25) -> list[bytes]:
    """Любой звук → OGG/Opus 16 кГц моно частями до `seconds` с (ffmpeg)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in"
        src.write_bytes(audio)
        cmd = [ffmpeg_exe() or "ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-c:a", "libopus",
               "-b:a", "24k", "-f", "segment", "-segment_time", str(seconds), str(Path(tmp) / "part%03d.ogg")]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        except OSError as e:
            raise VoiceError("FFmpeg для SpeechKit недоступен") from e
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise VoiceError("Не удалось прочитать аудиофайл") from e
        return [p.read_bytes() for p in sorted(Path(tmp).glob("part*.ogg"))]


def _transcribe_yandex(audio: bytes, client: httpx.Client | None) -> str:
    s = get_settings()
    folder, key = _speechkit_credentials(s)
    if not folder:
        raise VoiceError("Не задан каталог Yandex Cloud (BIS_YANDEX_FOLDER_ID)")
    if not key:
        raise VoiceError("Не задан API-ключ Yandex Cloud для распознавания речи")
    chunks = _ogg_chunks(audio)
    if len(chunks) > 40:
        raise VoiceError("Запись слишком длинная: разделите её на несколько частей")
    http = client or httpx.Client(timeout=60)
    try:
        parts = []
        for chunk in chunks:
            try:
                r = http.post(YANDEX_STT, params={"folderId": folder, "lang": "ru-RU", "format": "oggopus", "topic": "general"},
                              headers={"Authorization": f"Api-Key {key}"}, content=chunk)
            except httpx.HTTPError as e:
                raise VoiceError(f"SpeechKit недоступен: {e}") from e
            if r.status_code >= 400:
                raise VoiceError(f"SpeechKit отклонил запрос (HTTP {r.status_code}). Проверьте настройку распознавания речи.")
            parts.append(r.json().get("result", ""))
        return " ".join(p for p in parts if p).strip()
    finally:
        if client is None:
            http.close()


def transcribe(audio: bytes, filename: str = "audio.webm", mime: str | None = None,
               client: httpx.Client | None = None) -> str:
    s = get_settings()
    if s.stt_provider == "yandex":
        return normalize_dictation(_transcribe_yandex(audio, client))
    url, key = _openai_stt(s)
    if not url:
        raise VoiceError("Распознавание речи не настроено (BIS_STT_URL)")
    if url == OPENAI_STT_URL and not key:
        raise VoiceError("Не задан API-ключ OpenAI для распознавания речи")
    model = s.stt_model
    if url == OPENAI_STT_URL and (not s.stt_url or "stt_model" not in s.model_fields_set):
        model = OPENAI_STT_MODEL
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    http = client or httpx.Client(timeout=120)
    try:
        try:
            r = http.post(url + "/audio/transcriptions", headers=headers,
                          files={"file": (filename, audio, mime or "application/octet-stream")},
                          data={"model": model, "language": s.stt_language, "response_format": "json"})
        except httpx.HTTPError as e:
            raise VoiceError(f"Сервер распознавания недоступен: {e}") from e
        if r.status_code >= 400:
            raise VoiceError(f"Ошибка распознавания: HTTP {r.status_code}")
        text = r.json().get("text", "") if "json" in r.headers.get("content-type", "") else r.text
        return normalize_dictation(text)
    finally:
        if client is None:
            http.close()


def speak(text: str, client: httpx.Client | None = None) -> tuple[bytes, str]:
    s = get_settings()
    if not s.tts_url:
        raise VoiceError("Синтез речи не настроен (BIS_TTS_URL)")
    headers = {"Authorization": f"Bearer {s.tts_api_key}"} if s.tts_api_key else {}
    http = client or httpx.Client(timeout=120)
    try:
        try:
            r = http.post(s.tts_url.rstrip("/") + "/audio/speech", headers=headers,
                          json={"model": s.tts_model, "voice": s.tts_voice, "input": text[:4000], "response_format": "mp3"})
        except httpx.HTTPError as e:
            raise VoiceError(f"Сервер синтеза недоступен: {e}") from e
        if r.status_code >= 400:
            raise VoiceError(f"Ошибка синтеза: HTTP {r.status_code}")
        return r.content, r.headers.get("content-type", "audio/mpeg")
    finally:
        if client is None:
            http.close()
