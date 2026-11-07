"""Голос: распознавание речи (STT) и синтез (TTS) через OpenAI-совместимые серверы в закрытом контуре
(faster-whisper / speaches, vLLM Whisper; Silero/Piper за совместимым API). Аудио не уходит во внешние облака.

После распознавания текст нормализуется под строительную диктовку: «кубов» → «м3», «квадратов» → «м2» и т.п.
"""

from __future__ import annotations

import re

import httpx

from bis.config import get_settings

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


def status() -> dict[str, bool]:
    s = get_settings()
    return {"stt": bool(s.stt_url), "tts": bool(s.tts_url)}


def transcribe(audio: bytes, filename: str = "audio.webm", mime: str | None = None,
               client: httpx.Client | None = None) -> str:
    s = get_settings()
    if not s.stt_url:
        raise VoiceError("Распознавание речи не настроено (BIS_STT_URL)")
    headers = {"Authorization": f"Bearer {s.stt_api_key}"} if s.stt_api_key else {}
    http = client or httpx.Client(timeout=120)
    try:
        r = http.post(s.stt_url.rstrip("/") + "/audio/transcriptions", headers=headers,
                      files={"file": (filename, audio, mime or "application/octet-stream")},
                      data={"model": s.stt_model, "language": s.stt_language, "response_format": "json"})
    except httpx.HTTPError as e:
        raise VoiceError(f"Сервер распознавания недоступен: {e}") from e
    if r.status_code >= 400:
        raise VoiceError(f"Ошибка распознавания: HTTP {r.status_code}")
    text = r.json().get("text", "") if "json" in r.headers.get("content-type", "") else r.text
    return normalize_dictation(text)


def speak(text: str, client: httpx.Client | None = None) -> tuple[bytes, str]:
    s = get_settings()
    if not s.tts_url:
        raise VoiceError("Синтез речи не настроен (BIS_TTS_URL)")
    headers = {"Authorization": f"Bearer {s.tts_api_key}"} if s.tts_api_key else {}
    http = client or httpx.Client(timeout=120)
    try:
        r = http.post(s.tts_url.rstrip("/") + "/audio/speech", headers=headers,
                      json={"model": s.tts_model, "voice": s.tts_voice, "input": text[:4000], "response_format": "mp3"})
    except httpx.HTTPError as e:
        raise VoiceError(f"Сервер синтеза недоступен: {e}") from e
    if r.status_code >= 400:
        raise VoiceError(f"Ошибка синтеза: HTTP {r.status_code}")
    return r.content, r.headers.get("content-type", "audio/mpeg")
